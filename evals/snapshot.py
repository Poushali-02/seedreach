"""Save each eval company's website once and pre-fill an answer key to correct by hand.

    python -m evals.snapshot            # reads evals/data/companies.csv
    python -m evals.snapshot --refresh  # download pages again, even if already saved

For every company it writes evals/data/pages/<domain>.json (the scraped page plus the model's
answer), then evals/data/labels.csv: one row per company, pre-filled with the model's answer.
Rows already in labels.csv are kept as they are, so re-running never loses your corrections.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional

from src.config import ROOT, get_settings
from src.intelligence.extractor import ExtractionError, Extractor
from src.intelligence.scraper import ScrapedPage, ScrapeError, Scraper
from src.utils import domain_of

DATA = ROOT / "evals" / "data"
LABEL_FIELDS = (
    "domain", "url", "source", "case", "checked", "right_website", "company_name", "core_domain",
    "founder_first_name", "founder_last_name", "emails_on_site", "model_hook", "hook_ok", "notes",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    # utf-8-sig so Excel shows accented names correctly
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=LABEL_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def snapshot(row: dict[str, str], scraper: Scraper, extractor: Extractor, pages_dir: Path,
             refresh: bool = False) -> dict[str, Any]:
    """Return the saved snapshot for one company, scraping or extracting only what is missing."""
    path = pages_dir / f"{domain_of(row['url'])}.json"
    snap: dict[str, Any] = {} if refresh or not path.exists() else json.loads(path.read_text(encoding="utf-8"))
    snap.setdefault("url", row["url"])

    if not snap.get("page"):
        try:
            snap["page"], snap["error"] = asdict(scraper.fetch(row["url"])), None
        except ScrapeError as exc:
            snap["page"], snap["error"] = None, f"scrape failed: {exc}"
    if snap["page"] and not snap.get("prediction"):  # a failed LLM call is retried on the next run
        try:
            company = extractor.extract(ScrapedPage(**snap["page"]), row.get("name_hint") or None)
            snap["prediction"], snap["error"] = company.model_dump(), None
        except ExtractionError as exc:
            snap["prediction"], snap["error"] = None, f"extraction failed: {exc}"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    return snap


def label_row(row: dict[str, str], snap: dict[str, Any]) -> dict[str, str]:
    """An answer-key row pre-filled with the model's answer, for a human to correct."""
    page, pred = snap.get("page") or {}, snap.get("prediction") or {}
    return {
        "domain": domain_of(row["url"]),
        "url": row["url"],
        "source": row.get("source", ""),
        "case": row.get("case", ""),
        "checked": "",
        "right_website": "yes" if page else "",
        "company_name": pred.get("company_name", ""),
        "core_domain": pred.get("core_domain", ""),
        "founder_first_name": pred.get("founder_first_name") or "",
        "founder_last_name": pred.get("founder_last_name") or "",
        "emails_on_site": " ".join(page.get("mailtos", [])),
        "model_hook": pred.get("personalized_hook", ""),
        "hook_ok": "",
        "notes": snap.get("error") or "",
    }


def merge_labels(new: list[dict[str, str]], existing: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep existing rows, except untouched placeholders left by a failed scrape or LLM call."""
    old = {r["domain"]: r for r in existing}
    merged = []
    for row in new:
        prev = old.get(row["domain"])
        merged.append(prev if prev and (prev.get("checked") or prev.get("company_name")) else row)
    seen = {r["domain"] for r in merged}
    return merged + [r for r in existing if r["domain"] not in seen]


def run(companies: list[dict[str, str]], scraper: Scraper, extractor: Extractor, data: Path,
        refresh: bool = False, echo: Optional[Callable[[str], None]] = print) -> list[dict[str, str]]:
    rows = []
    for i, row in enumerate(companies, 1):
        snap = snapshot(row, scraper, extractor, data / "pages", refresh)
        pred = snap.get("prediction") or {}
        founder = " ".join(filter(None, (pred.get("founder_first_name"), pred.get("founder_last_name")))) or "-"
        status = snap.get("error") or f"{pred.get('company_name')} | {pred.get('core_domain')} | founder: {founder}"
        if echo:
            echo(f"[{i:2}/{len(companies)}] {domain_of(row['url'])}: {status}")
        rows.append(label_row(row, snap))

    labels_path = data / "labels.csv"
    merged = merge_labels(rows, read_csv(labels_path) if labels_path.exists() else [])
    write_csv(labels_path, merged)
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--companies", type=Path, default=DATA / "companies.csv",
                        help="CSV with columns url, source, case, name_hint")
    parser.add_argument("--refresh", action="store_true", help="download pages again, even if already saved")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    settings = get_settings()
    rows = run(read_csv(args.companies), Scraper(settings), Extractor(settings), DATA, args.refresh)
    print(f"\nWrote {DATA / 'labels.csv'} ({len(rows)} rows). Open it, correct each row, and put 'yes' in 'checked'.")


if __name__ == "__main__":
    main()
