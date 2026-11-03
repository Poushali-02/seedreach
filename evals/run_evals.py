"""Score seedreach's extraction against the hand-checked answer key in evals/data/labels.csv.

    python -m evals.run_evals                # score the answers saved by evals.snapshot (no API calls)
    python -m evals.run_evals --live -n 3    # re-run the extractor 3 times on each saved page

Only rows with checked = yes are scored. Results are printed and saved under evals/data/results/.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from src.config import get_settings
from src.intelligence.extractor import ExtractionError, Extractor
from src.intelligence.scraper import ScrapedPage
from src.utils import domain_of

from evals.snapshot import DATA, read_csv

_LEGAL_SUFFIX = re.compile(r"\b(inc|llc|ltd|gmbh|oy|ab|corp|co|technologies)\b\.?", re.I)
_FLATTERY = re.compile(r"\b(i love|impressive|amazing|exciting)\b", re.I)
_SENTENCE_END = re.compile(r"[.!?](\s|$)")


def load_labels(path: Path) -> list[dict[str, str]]:
    """Checked rows of the answer key. Tolerates spreadsheet padding around values."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f, skipinitialspace=True)]
    return [r for r in rows if r.get("checked", "").lower() == "yes"]


def norm_name(name: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", _LEGAL_SUFFIX.sub("", (name or "").casefold()))


def domain_ok(truth: str, pred: Optional[str]) -> bool:
    """The answer key may allow several domains, written as 'DevTools|AI Agents'."""
    return (pred or "") in {d.strip() for d in truth.split("|")}


def founder_outcome(truth: tuple[str, str], pred: tuple[Optional[str], Optional[str]],
                    others: str = "") -> str:
    """correct | partial (right first name only) | missed | invented | wrong_person.

    `others` lists the site's other founders ("Sam Bobb; Max"): naming any of them is also correct.
    """
    t_first, t_last = (norm_name(x) for x in truth)
    p_first, p_last = (norm_name(x) for x in pred)
    if not t_first:
        return "correct" if not p_first else "invented"
    if not p_first:
        return "missed"
    for other in filter(None, (o.strip() for o in others.split(";"))):
        o_first, _, o_last = other.partition(" ")
        if p_first == norm_name(o_first) and p_last == norm_name(o_last):
            return "correct"
    if p_first != t_first:
        return "wrong_person"
    return "correct" if p_last == t_last else "partial"


def hook_rule_problems(hook: str) -> list[str]:
    """Checks the prompt's rules that need no judgement: one sentence, no flattery, no markdown."""
    problems = []
    if len(_SENTENCE_END.findall(hook.strip())) > 1:
        problems.append("more than one sentence")
    if _FLATTERY.search(hook):
        problems.append("flattery")
    if re.search(r"[*#`]", hook):
        problems.append("markdown")
    return problems


def score_row(label: dict[str, str], pred: Optional[dict[str, Any]], hook_ok: Optional[bool]) -> dict[str, Any]:
    """Compare one model answer with the answer key. hook_ok is the human verdict when it applies."""
    if pred is None:
        return {"domain": label["domain"], "failed": True}
    right_site = label.get("right_website", "yes").lower() != "no"
    result = {
        "domain": label["domain"],
        "failed": False,
        "right_website": right_site,
        "name_ok": norm_name(pred.get("company_name")) == norm_name(label["company_name"]),
        "domain_ok": domain_ok(label["core_domain"], pred.get("core_domain")),
        "founder": founder_outcome(
            (label.get("founder_first_name", ""), label.get("founder_last_name", "")),
            (pred.get("founder_first_name"), pred.get("founder_last_name")),
            label.get("other_founders", ""),
        ),
        "hook_rule_problems": hook_rule_problems(pred.get("personalized_hook", "")),
        "hook_ok": hook_ok,
        "flagged_not_company": pred.get("page_type", "company") != "company",
        "pred_domain": pred.get("core_domain"),
        "true_domain": label["core_domain"],
    }
    result["sendable"] = bool(
        right_site and not result["flagged_not_company"] and result["name_ok"] and result["domain_ok"] and result["founder"] == "correct"
        and not result["hook_rule_problems"] and hook_ok is not False
    )
    return result


def pct(n: int, d: int) -> Optional[float]:
    return round(100 * n / d, 1) if d else None


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Field scores count only pages on the company's own site; 'sendable' counts every page."""
    scored = [r for r in results if not r["failed"]]
    on_site = [r for r in scored if r["right_website"]]
    founders = Counter(r["founder"] for r in on_site)
    judged = [r for r in on_site if r["hook_ok"] is not None]
    return {
        "rows": len(results),
        "failed_calls": len(results) - len(scored),
        "wrong_website_rows": len(scored) - len(on_site),
        "wrong_website_caught": pct(sum(r["flagged_not_company"] for r in scored if not r["right_website"]),
                                    len(scored) - len(on_site)),
        "company_site_flagged": sum(r["flagged_not_company"] for r in on_site),
        "company_name": pct(sum(r["name_ok"] for r in on_site), len(on_site)),
        "core_domain": pct(sum(r["domain_ok"] for r in on_site), len(on_site)),
        "founder_correct": pct(founders["correct"], len(on_site)),
        "founder_outcomes": dict(founders),
        "invented_founder": pct(founders["invented"], len(on_site)),
        "hook_rules_ok": pct(sum(not r["hook_rule_problems"] for r in on_site), len(on_site)),
        "hook_grounded": pct(sum(r["hook_ok"] for r in judged), len(judged)),
        "sendable": pct(sum(r["sendable"] for r in scored), len(scored)),
        "domain_confusions": dict(Counter(
            f"{r['true_domain']} -> {r['pred_domain']}" for r in on_site if not r["domain_ok"]
        )),
    }


def score_saved(labels: list[dict[str, str]], pages_dir: Path) -> list[dict[str, Any]]:
    """Score the answers evals.snapshot saved: the ones a human read, so hook_ok applies."""
    results = []
    for label in labels:
        snap = json.loads((pages_dir / f"{label['domain']}.json").read_text(encoding="utf-8"))
        pred = snap.get("prediction")
        # the verdict is about the hook the human read; a re-scraped page has a new one
        same_hook = pred is not None and pred.get("personalized_hook") == label.get("model_hook")
        verdict = label.get("hook_ok", "").lower() if same_hook else ""
        results.append(score_row(label, pred, {"yes": True, "no": False}.get(verdict)))
    return results


def score_live(labels: list[dict[str, str]], pages_dir: Path, extractor: Extractor, repeats: int,
               hints: dict[str, str], echo: Optional[Callable[[str], None]] = print) -> list[list[dict[str, Any]]]:
    """Re-run the extractor on every saved page. Fresh hooks have no human verdict yet."""
    runs: list[list[dict[str, Any]]] = [[] for _ in range(repeats)]
    for i, label in enumerate(labels, 1):
        snap = json.loads((pages_dir / f"{label['domain']}.json").read_text(encoding="utf-8"))
        page = ScrapedPage(**snap["page"])
        for run in runs:
            try:
                pred: Optional[dict[str, Any]] = extractor.extract(page, hints.get(label["domain"])).model_dump()
            except ExtractionError:
                pred = None
            run.append(score_row(label, pred, None))
        if echo:
            echo(f"[{i:2}/{len(labels)}] {label['domain']}")
    return runs


def stability(runs: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Rows whose domain or founder verdict changed between repeats."""
    unstable = []
    for rows in zip(*runs):
        if any(r["failed"] for r in rows):
            unstable.append(rows[0]["domain"])
        elif len({(r["pred_domain"], r["founder"]) for r in rows}) > 1:
            unstable.append(rows[0]["domain"])
    return {"unstable_rows": unstable, "stable": round(100 * (1 - len(unstable) / len(runs[0])), 1)}


def print_report(summary: dict[str, Any], title: str) -> None:
    lines = [
        f"\n{title}",
        f"  rows scored            {summary['rows']}  (failed calls: {summary['failed_calls']},"
        f" wrong-website pages: {summary['wrong_website_rows']})",
        f"  company name           {summary['company_name']}%",
        f"  core domain            {summary['core_domain']}%",
        f"  founder correct        {summary['founder_correct']}%   {summary['founder_outcomes']}",
        f"  invented founder       {summary['invented_founder']}%",
        f"  hook follows rules     {summary['hook_rules_ok']}%",
        f"  hook grounded (human)  {summary['hook_grounded']}%" if summary["hook_grounded"] is not None else "",
        f"  wrong website caught   {summary['wrong_website_caught']}%   "
        f"(company sites wrongly flagged: {summary['company_site_flagged']})",
        f"  READY TO SEND          {summary['sendable']}%",
    ]
    if summary["domain_confusions"]:
        lines.append(f"  domain mix-ups         {summary['domain_confusions']}")
    print("\n".join(line for line in lines if line))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--labels", type=Path, default=DATA / "labels.csv")
    parser.add_argument("--pages", type=Path, default=DATA / "pages", help="saved pages to score against")
    parser.add_argument("--live", action="store_true", help="re-run the extractor instead of using saved answers")
    parser.add_argument("-n", "--repeats", type=int, default=3, help="runs per page with --live")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    labels = load_labels(args.labels)
    out: dict[str, Any] = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"), "labels": len(labels)}
    if args.live:
        settings = get_settings()
        out["model"] = settings.gemini_model if settings.llm_provider == "gemini" else settings.anthropic_model
        hints = {domain_of(r["url"]): r.get("name_hint") or None for r in read_csv(DATA / "companies.csv")}
        runs = score_live(labels, args.pages, Extractor(settings), args.repeats, hints)
        out["runs"] = [summarize(r) for r in runs]
        out["stability"] = stability(runs)
        for i, summary in enumerate(out["runs"], 1):
            print_report(summary, f"Live run {i}/{args.repeats} ({out['model']})")
        print(f"\nSame domain and founder verdict in every run: {out['stability']['stable']}%"
              f"  unstable: {', '.join(out['stability']['unstable_rows']) or 'none'}")
    else:
        results = score_saved(labels, args.pages)
        out["summary"], out["rows"] = summarize(results), results
        print_report(out["summary"], "Saved answers (the ones you checked)")

    results_dir = DATA / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{datetime.now():%Y-%m-%d-%H%M}-{'live' if args.live else 'saved'}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
