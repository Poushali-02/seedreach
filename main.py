"""seedreach CLI.

    python main.py run --limit 5
    python main.py target https://useshiplog.com
    python main.py add-target https://useshiplog.com
    python main.py status

Drafts are only ever saved to Gmail Drafts; nothing is sent automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from src.config import get_settings
from src.db import Database
from src.digest import build_digest
from src.intelligence.extractor import ExtractionError
from src.pipeline import Pipeline, Result
from src.trace import Trace
from src.utils import domain_of, linkedin_people_search, normalize_url

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Cold-outreach drafts for early-stage startups.")
console = Console()
_STYLE = {"drafted": "green", "skipped": "yellow", "duplicate": "dim", "error": "red"}


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    level = logging.DEBUG if verbose else getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s", handlers=[RichHandler(console=console, show_path=False)])
    for noisy in ("httpx", "httpcore", "googleapiclient", "urllib3", "anthropic", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _db() -> Database:
    return Database(get_settings().db_path)


def _pipeline(db: Database, dry_run: bool) -> Pipeline:
    try:
        return Pipeline(get_settings(), db, dry_run=dry_run)
    except (ExtractionError, Exception) as exc:  # missing key, OAuth failure, ...
        console.print(f"[red]Setup failed:[/red] {exc}")
        raise typer.Exit(1)


def _report(result: Result) -> None:
    style = _STYLE.get(result.outcome, "white")
    to = f" -> {result.to}" if result.to else ""
    console.print(f"[{style}]{result.outcome.upper():9}[/{style}] {result.domain}{to}  [dim]{result.detail}[/dim]")
    if result.draft and "dry run" in result.detail:
        console.print(Panel(result.draft.body, title=f"Subject: {result.draft.subject}", expand=False))


def _summary(results: list[Result], dry_run: bool) -> dict[str, int]:
    counts = {k: sum(r.outcome == k for r in results) for k in ("drafted", "skipped", "error")}
    console.print(f"\nDone: {counts['drafted']} drafted, {counts['skipped']} skipped, {counts['error']} errors.")
    if dry_run:
        console.print("Dry run: nothing was saved to Gmail or the database.")
    elif counts["drafted"]:
        console.print("Review them in Gmail > Drafts. Nothing has been sent.")
    return counts


def _cost(input_tokens: int, output_tokens: int) -> str:
    s = get_settings()
    if not (s.llm_price_input_per_m or s.llm_price_output_per_m):
        return "-"  # prices not set (free tier, or not configured)
    return f"${(input_tokens * s.llm_price_input_per_m + output_tokens * s.llm_price_output_per_m) / 1e6:.4f}"


def _run_stats(traces: list[Trace]) -> None:
    """Where the time and tokens went, and what may need a look."""
    if not traces:
        return
    tokens_in, tokens_out = sum(t.input_tokens for t in traces), sum(t.output_tokens for t in traces)
    seconds = sum(sum(t.ms.values()) for t in traces) / 1000
    console.print(
        f"[dim]{len(traces)} companies in {seconds:.0f}s ({seconds / len(traces):.1f}s each), "
        f"{tokens_in:,} tokens in / {tokens_out:,} out, cost {_cost(tokens_in, tokens_out)}[/dim]"
    )
    cut = [t.domain for t in traces if t.truncated]
    unseen = [t.domain for t in traces if t.founder_in_text is False]
    retried = [t.domain for t in traces if t.llm_calls > 1]
    for label, domains in (("text cut at the length limit", cut), ("founder name not on the page", unseen),
                           ("LLM output retried", retried)):
        if domains:
            console.print(f"[dim]  {label}: {', '.join(domains)}[/dim]")


@app.command()
def run(
    limit: int = typer.Option(5, "--limit", "-n", min=1, help="Stop after this many drafts."),
    source: str = typer.Option("all", "--source", "-s", help="all | yc | launchhn | hn | producthunt"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print drafts; skip Gmail and don't touch the database."),
    digest: Optional[Path] = typer.Option(None, "--digest", help="Also write a short Markdown summary here: roles "
                                          "to apply for, founders to reach, drafts to review (no email addresses)."),
) -> None:
    """Fetch new leads, enrich them, and save drafts to Gmail."""
    pipe = _pipeline(_db(), dry_run)
    try:
        results = pipe.run(limit, source, on_result=_report)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    if digest is not None:  # written even when empty, so a scheduler can tell "nothing new" from "didn't run"
        digest.write_text(build_digest(results, dry_run=dry_run), encoding="utf-8")
    counts = _summary(results, dry_run)
    _run_stats(pipe.traces)
    if counts["error"] and not counts["drafted"]:
        raise typer.Exit(1)  # every attempt failed (LLM or Gmail outage): let schedulers flag the run


@app.command()
def target(
    url: str = typer.Argument(..., help="Startup website, e.g. https://useshiplog.com"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the draft; skip Gmail and the database."),
    force: bool = typer.Option(False, "--force", help="Retry a company previously skipped."),
) -> None:
    """Run the full pipeline for one specific startup."""
    pipe = _pipeline(_db(), dry_run)
    result = pipe.process(url, "manual", force=force)
    _report(result)
    if result.outcome == "error":
        raise typer.Exit(1)


@app.command("add-target")
def add_target(urls: list[str] = typer.Argument(..., help="One or more startup URLs or domains.")) -> None:
    """Queue URLs/domains; the next `run` processes them before any sourced leads."""
    db = _db()
    for raw in urls:
        try:
            url = normalize_url(raw)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            continue
        domain = domain_of(url)
        if db.queue(domain, url, "manual"):
            console.print(f"[green]queued[/green]  {domain}")
        else:
            console.print(f"[yellow]already tracked[/yellow]  {domain} ({db.status_of(domain)})")


@app.command()
def status(events: int = typer.Option(10, "--events", "-e", help="How many recent log events to show.")) -> None:
    """Show totals, recent drafts and the event log."""
    db = _db()
    s = db.stats()
    console.print(
        f"Companies tracked: [bold]{s['total']}[/bold]   Drafts created: [green]{s['drafted']}[/green]   "
        f"Skipped: [yellow]{s['skipped']}[/yellow]   Queued: {s['queued']}"
    )
    recent = db.recent_companies(10)
    if recent:
        table = Table(title="Recent companies")
        for col in ("Domain", "Status", "Founder email", "Email source", "Reason"):
            table.add_column(col)
        for r in recent:
            table.add_row(r["domain"], r["status"], r["founder_email"] or "-", r["email_source"] or "-", r["reason"] or "")
        console.print(table)
    runs = db.recent_runs(5)
    if runs:
        table = Table(title="Recent runs", caption="Done: drafted/skipped/errors. Tokens: in/out. "
                      "Cut: text past the length limit. Off-page: founder named but not on the site.")
        for col in ("Run (UTC)", "Done", "Tokens", "Cost", "Avg", "Cut", "Off-page"):
            table.add_column(col)
        for r in runs:
            table.add_row(
                r["run_id"][5:16].replace("T", " "), f"{r['drafted']}/{r['skipped']}/{r['errors']}",
                f"{r['input_tokens'] / 1000:.1f}k/{r['output_tokens'] / 1000:.1f}k",
                _cost(r["input_tokens"], r["output_tokens"]), f"{(r['avg_ms'] or 0) / 1000:.1f}s",
                str(r["truncated"]), str(r["founder_not_in_text"]),
            )
        console.print(table)
    waiting = db.needs_contact(10)
    if waiting:
        table = Table(title="Needs a contact", caption="Reach the founder directly (the LinkedIn link searches for "
                      "them), or find an address you trust, then: python main.py target <url> --force")
        for col in ("Domain", "Company", "Founder", "Why", "Find them"):
            table.add_column(col)
        for r in waiting:
            find = linkedin_people_search(r["founder_name"], r["company_name"] or "") if r["founder_name"] else "-"
            table.add_row(r["domain"], r["company_name"] or "-", r["founder_name"] or "-", r["reason"], find)
        console.print(table)
    log_rows = db.recent_events(events)
    if log_rows:
        console.print("\n[bold]Recent events[/bold]")
        for e in reversed(log_rows):
            console.print(f"[dim]{e['ts']}[/dim] {e['level']:5} {e['domain'] or ''} {e['message']}")


if __name__ == "__main__":
    app()
