"""seedreach CLI.

    python main.py run --limit 5
    python main.py target https://useshiplog.com
    python main.py add-target https://useshiplog.com
    python main.py add-target --csv companies.csv
    python main.py status
    python main.py track
    python main.py mark useshiplog.com interview
    python main.py export --csv tracker.csv
    python main.py followups --dry-run

Drafts are only ever saved to Gmail Drafts; nothing is sent automatically.
"""

from __future__ import annotations

import csv as csv_mod
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from src.config import get_settings
from src.db import STAGES, Database
from src.digest import build_digest
from src.intelligence.extractor import ExtractionError
from src.notify import build_summary, send_summary
from src.outreach.gmail_client import GmailDraftClient, GmailError, token_can_read
from src.pipeline import Pipeline, Result
from src.trace import Trace
from src.followups import FollowUp, draft_followups
from src.profile import ProfileError, load_profile
from src.tracker import Change, update_stages
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


def _pipeline(db: Database, dry_run: bool, *, include_large: bool = False) -> Pipeline:
    try:
        return Pipeline(get_settings(), db, dry_run=dry_run, include_large=include_large)
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
    include_large: bool = typer.Option(False, "--include-large", help="Process growth/large companies instead of "
                                       "skipping them. (Does not unskip previously skipped companies; use --force on target for that.)"),
    digest: Optional[Path] = typer.Option(None, "--digest", help="Also write a short Markdown summary here: roles "
                                          "to apply for, founders to reach, drafts to review (no email addresses)."),
) -> None:
    """Fetch new leads, enrich them, and save drafts to Gmail."""
    pipe = _pipeline(_db(), dry_run, include_large=include_large)
    try:
        results = pipe.run(limit, source, on_result=_report)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    alerts = pipe.job_alerts()  # empty unless JOB_ALERTS=1
    if alerts:
        console.print(f"\n[bold]{len(alerts)} new from your job alerts[/bold] (apply yourself; no draft is made)")
        for a in alerts:
            console.print(f"  {a.company}: {a.role}{', ' + a.location if a.location else ''}  [dim]{a.apply_url}[/dim]")
    if not dry_run:
        _print_changes(pipe.track())
        _print_followups(pipe.follow_up())
    if digest is not None:  # written even when empty, so a scheduler can tell "nothing new" from "didn't run"
        digest.write_text(build_digest(results, dry_run=dry_run, alerts=alerts), encoding="utf-8")
    counts = _summary(results, dry_run)
    if not dry_run:  # before the exit below, so a failed run still reports
        send_summary(build_summary(results, len(alerts)), get_settings())
    _run_stats(pipe.traces)
    if counts["error"] and not counts["drafted"]:
        raise typer.Exit(1)  # every attempt failed (LLM or Gmail outage): let schedulers flag the run


@app.command()
def target(
    url: str = typer.Argument(..., help="Startup website, e.g. https://useshiplog.com"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the draft; skip Gmail and the database."),
    force: bool = typer.Option(False, "--force", help="Retry a company previously skipped."),
    include_large: bool = typer.Option(False, "--include-large", help="Process growth/large companies instead of "
                                       "skipping them. (Does not unskip previously skipped companies; use --force for that.)"),
) -> None:
    """Run the full pipeline for one specific startup."""
    pipe = _pipeline(_db(), dry_run, include_large=include_large)
    result = pipe.process(url, "manual", force=force)
    _report(result)
    if result.outcome == "error":
        raise typer.Exit(1)


@app.command("add-target")
def add_target(
    urls: Optional[list[str]] = typer.Argument(None, help="One or more startup URLs or domains."),
    csv: Optional[Path] = typer.Option(None, "--csv", "-c", help="Import URLs from a CSV file with a url or website column."),
) -> None:
    """Queue URLs/domains; the next `run` processes them before any sourced leads."""
    if not urls and not csv:
        console.print("[red]Provide at least one URL or specify a CSV file with --csv.[/red]")
        raise typer.Exit(2)

    db = _db()
    queued = 0
    already_tracked = 0
    invalid = 0

    def _enqueue(raw: str, prefix: str = "") -> None:
        nonlocal queued, already_tracked, invalid
        raw = raw.strip()
        if not raw:
            if prefix:
                console.print(f"[yellow]{prefix}empty URL, skipping[/yellow]")
            else:
                console.print("[red]Empty URL provided[/red]")
            invalid += 1
            return
        try:
            url = normalize_url(raw)
        except ValueError as exc:
            console.print(f"[red]{prefix}{exc}[/red]")
            invalid += 1
            return
        domain = domain_of(url)
        if db.queue(domain, url, "manual"):
            queued += 1
            console.print(f"[green]queued[/green]  {domain}")
        else:
            already_tracked += 1
            console.print(f"[yellow]already tracked[/yellow]  {domain} ({db.status_of(domain)})")

    if csv is not None:
        if not csv.is_file():
            console.print(f"[red]File not found: {csv}[/red]")
            raise typer.Exit(1)
        # newline="": the csv module's own line handling, so a line break inside a quoted cell stays in the cell
        with csv.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv_mod.DictReader(f)
            if not reader.fieldnames:
                console.print(f"[red]Empty CSV file: {csv}[/red]")
                raise typer.Exit(1)
            url_col = None
            for candidate in ("url", "website"):
                for col in reader.fieldnames:
                    if col and col.strip().lower() == candidate:
                        url_col = col
                        break
                if url_col:
                    break
            if not url_col:
                console.print(f"[red]No 'url' or 'website' column found in {csv}[/red]")
                raise typer.Exit(1)
            for row in reader:
                _enqueue(row.get(url_col) or "", prefix=f"Row {reader.line_num}: ")

    if urls:
        for raw in urls:
            _enqueue(raw)

    console.print(f"\nDone: {queued} queued, {already_tracked} already tracked, {invalid} invalid.")


def _print_changes(changes: list[Change]) -> None:
    if changes:
        console.print(f"\n[bold]Tracker[/bold]: {len(changes)} update{'s' if len(changes) != 1 else ''}")
        for c in changes:
            console.print(f"  {escape(c.company)}: [bold]{c.stage}[/bold] [dim]({c.at[:10]})[/dim]")


@app.command()
def track() -> None:
    """Check your mailbox for drafts you've sent, and for replies and bounces; record each company's stage."""
    settings = get_settings()
    if not settings.track_mailbox:
        console.print("TRACK_MAILBOX is off, so nothing is checked.")
        return
    can_read = settings.job_alerts or token_can_read(settings.google_token_file)
    gmail = GmailDraftClient(settings.google_credentials_file, settings.google_token_file, settings.headless,
                             read_mail=can_read)
    try:
        gmail.ensure_authenticated()
    except GmailError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if not can_read:
        console.print("[yellow]Your Gmail token can only write drafts, so only drafts that left Drafts are seen "
                      "('draft gone'). Sent, replies and bounces need read access: run once with JOB_ALERTS=1 "
                      "to sign in with it.[/yellow]")
    changes = update_stages(_db(), gmail, can_read)
    _print_changes(changes)
    if not changes:
        console.print("No changes.")


@app.command()
def mark(
    domain: str = typer.Argument(..., help="The company's website or domain."),
    stage: str = typer.Argument(..., help=" | ".join(STAGES)),
) -> None:
    """Set where a drafted company stands yourself, e.g. interview, offer or closed."""
    try:
        name = domain_of(normalize_url(domain))
        found = _db().set_stage(name, stage.lower(), "manual")
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    if not found:
        console.print(f"[yellow]{name} has no draft, so there's nothing to track.[/yellow]")
        raise typer.Exit(1)
    console.print(f"{name}: [bold]{stage.lower()}[/bold]")


def _print_followups(done: list[FollowUp], dry_run: bool = False) -> None:
    if not done:
        return
    verb = "would be drafted" if dry_run else "drafted (review them in Gmail > Drafts)"
    console.print(f"\n[bold]{len(done)} follow-up{'s' if len(done) != 1 else ''}[/bold] {verb}: "
                  + ", ".join(escape(f.company) for f in done))
    if dry_run:
        for f in done:
            console.print(Panel(escape(f.draft.body), title=escape(f"Subject: {f.draft.subject}"), expand=False))


@app.command()
def followups(
    after_days: Optional[int] = typer.Option(None, "--after-days", help="Follow up on emails sent at least this "
                                             "many days ago (default: FOLLOW_UP_AFTER_DAYS, 7)."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="At most this many (default: "
                                        "FOLLOW_UPS_PER_RUN, 5)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the follow-ups; create no drafts, record nothing."),
) -> None:
    """Draft one short follow-up, in the same Gmail conversation, for companies that haven't replied."""
    settings = get_settings()
    if not (settings.job_alerts or token_can_read(settings.google_token_file)):
        console.print("[yellow]Follow-ups need to know which emails you sent, which needs Gmail read access. "
                      "Run once with JOB_ALERTS=1 to sign in with it, then `python main.py track`.[/yellow]")
        raise typer.Exit(1)
    try:
        profile = load_profile()
    except ProfileError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)
    gmail = GmailDraftClient(settings.google_credentials_file, settings.google_token_file, settings.headless,
                             read_mail=True)
    try:
        gmail.ensure_authenticated()
    except GmailError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    days = settings.follow_up_after_days if after_days is None else after_days
    done = draft_followups(_db(), gmail, profile, days, settings.follow_ups_per_run if limit is None else limit,
                           dry_run=dry_run)
    _print_followups(done, dry_run)
    if not done:
        console.print(f"No follow-ups due: nobody emailed {days}+ days ago is waiting without one. "
                      "(Run `python main.py track` first so recent sends and replies are known.)")


EXPORT_COLUMNS = ("domain", "company_name", "founder_name", "founder_email", "email_source", "stage", "stage_source",
                  "drafted_at", "sent_at", "replied_at", "stage_at", "followed_up_at")


@app.command()
def export(csv: Path = typer.Option(..., "--csv", help="Where to write the CSV (it holds email addresses: "
                                                      "keep it out of git).")) -> None:
    """Write every drafted company and where it stands to a CSV, for a spreadsheet."""
    rows = _db().drafted()
    with csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv_mod.writer(f)
        writer.writerow(EXPORT_COLUMNS)
        writer.writerows([r[c] for c in EXPORT_COLUMNS] for r in rows)
    console.print(f"Wrote {len(rows)} compan{'y' if len(rows) == 1 else 'ies'} to {csv}")


@app.command()
def status(events: int = typer.Option(10, "--events", "-e", help="How many recent log events to show.")) -> None:
    """Show totals, the tracker, recent drafts, recent runs and the event log."""
    db = _db()
    s = db.stats()
    console.print(
        f"Companies tracked: [bold]{s['total']}[/bold]   Drafts created: [green]{s['drafted']}[/green]   "
        f"Skipped: [yellow]{s['skipped']}[/yellow]   Queued: {s['queued']}"
    )
    stages = db.stage_counts()
    if set(stages) - {"drafted"}:
        console.print("Tracker: " + "   ".join(f"{k}: {stages[k]}" for k in ("drafted", *STAGES) if stages.get(k)))
        moving = db.tracked(("replied", "interview", "offer", "bounced"), 10)
        if moving:
            table = Table(title="Replies and next steps")
            for col in ("Company", "Stage", "Since", "Address"):
                table.add_column(col)
            for r in moving:
                table.add_row(escape(r["company_name"] or r["domain"]), r["stage"], (r["stage_at"] or "")[:10],
                              escape(r["founder_email"] or "-"))
            console.print(table)
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
