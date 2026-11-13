"""Orchestrates scrape -> extract -> enrich -> generate -> Gmail draft, with dedupe."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

from src.config import Settings
from src.db import Database
from src.enrichment.email_finder import EmailFinder, EmailResult
from src.enrichment.company_facts import CompanyFactsClient
from src.enrichment.job_boards import JobBoardChecker
from src.intelligence.extractor import ExtractionError, Extractor
from src.intelligence.scraper import ScrapeError, Scraper
from src.outreach.generator import EmailDraft, generate_email
from src.outreach.gmail_client import GmailDraftClient, GmailError
from src.profile import load_profile
from src.sourcing.base import BaseSourcingProvider, Lead, parse_preferences, region_rank
from src.sourcing.hacker_news import HackerNewsProvider
from src.sourcing.launch_hn import LaunchHNProvider
from src.sourcing.product_hunt import ProductHuntProvider
from src.sourcing.yc_directory import YCDirectoryProvider
from src.trace import Trace, name_in_text
from src.utils import domain_of, non_company_kind, normalize_url

log = logging.getLogger(__name__)

# Outcomes: drafted | skipped (recorded, never retried) | duplicate (already known) | error (not recorded, retryable)
Reporter = Callable[["Result"], None]
NEEDS_CONTACT = "needs contact"  # skip reason prefix: a company worth writing to, but no address we trust
FOUNDER_INBOXES = {"founders", "founder"}  # generic-looking, but they reach the founders
_PAGE_KIND = {"job_board": "a job-board page", "news": "a news article", "other": "not a company website"}


def _not_company_site(kind: str, company: Optional[str] = None) -> str:
    about = f" about {company}" if company else ""
    return f"not the company's own site ({_PAGE_KIND.get(kind, kind)}{about}); add its website with `target`"


@dataclass
class Result:
    domain: str
    outcome: str
    detail: str
    draft: Optional[EmailDraft] = None
    to: Optional[str] = None
    # what we learned about the company, for the run digest (see src/digest.py)
    company: Optional[str] = None
    founder: Optional[str] = None
    role: Optional[str] = None  # an open engineering internship read from its job board
    apply_url: Optional[str] = None
    role_posted: Optional[date] = None  # when that opening was first posted, if the board says
    size: str = ""  # e.g. "Seed, ~7 people" (Apollo)


def build_providers(settings: Settings, names: str = "all") -> list[BaseSourcingProvider]:
    available: dict[str, Callable[[Settings], BaseSourcingProvider]] = {
        "yc": YCDirectoryProvider,
        "launchhn": LaunchHNProvider,
        "hn": HackerNewsProvider,
        "producthunt": ProductHuntProvider,
    }
    if names == "all":
        return [factory(settings) for factory in available.values()]
    if names not in available:
        raise ValueError(f"Unknown source {names!r}; choose from: all, {', '.join(available)}")
    return [available[names](settings)]


def interleave(streams: list[Iterator[Lead]]) -> Iterator[Lead]:
    """Take one lead from each source in turn, so a single run draws on all of them."""
    active = list(streams)
    while active:
        for stream in list(active):
            lead = next(stream, None)
            if lead is None:
                active.remove(stream)
            else:
                yield lead


def ranked_interleave(streams: list[Iterator[Lead]], rank: Callable[[Lead], int]) -> Iterator[Lead]:
    """Like interleave, but always take the best-ranked lead (lowest rank) waiting at the front of a source.

    Sources take turns among equally ranked leads. Each source should yield its best leads first (the
    YC provider sorts by the same preference); only one lead per source is read ahead, so it stays lazy.
    """
    heads: list[tuple[Iterator[Lead], Lead]] = []
    for stream in streams:
        lead = next(stream, None)
        if lead is not None:
            heads.append((stream, lead))
    turn = 0
    while heads:
        best = min(rank(lead) for _, lead in heads)
        order = list(range(turn, len(heads))) + list(range(turn))
        i = next(i for i in order if rank(heads[i][1]) == best)
        stream, lead = heads[i]
        yield lead
        following = next(stream, None)
        if following is None:
            heads.pop(i)
            turn = i
        else:
            heads[i] = (stream, following)
            turn = i + 1
        turn = turn % len(heads) if heads else 0


class Pipeline:
    def __init__(self, settings: Settings, db: Database, *, dry_run: bool = False) -> None:
        self.settings = settings
        self.db = db
        self.dry_run = dry_run
        self.run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.traces: list[Trace] = []  # one per company processed, kept in memory for the run summary
        self.profile = load_profile()
        self.scraper = Scraper(settings)
        self.extractor = Extractor(settings)
        self.finder = EmailFinder(settings)
        self.boards = JobBoardChecker(settings.http_timeout)
        self.company_facts = CompanyFactsClient(settings.apollo_api_key, settings.http_timeout)
        self.gmail = GmailDraftClient(settings.google_credentials_file, settings.google_token_file, settings.headless)
        if not dry_run:
            self.gmail.ensure_authenticated()  # fail before spending LLM credits

    def _record(self, level: str, domain: str, message: str) -> None:
        # the CLI already prints a status line per company, so routine events only go to debug output
        log.log(logging.DEBUG if level == "INFO" else getattr(logging, level), "%s: %s", domain, message)
        if not self.dry_run:
            self.db.log(level, message, domain)

    def _generic_only(self, email: EmailResult, founder: Optional[str]) -> bool:
        """A hello@-style inbox that GENERIC_INBOX says not to draft to. founders@ is the founders' own."""
        if email.confidence != "generic" or email.email.split("@")[0] in FOUNDER_INBOXES:
            return False
        mode = self.settings.generic_inbox
        return mode == "never" or (mode == "no_founder" and founder is not None)

    def _skip(self, domain: str, url: str, source: str, reason: str, **kw: Optional[str]) -> Result:
        if not self.dry_run:
            self.db.mark_skipped(domain, url, source, reason, **kw)
        self._record("INFO", domain, f"skipped: {reason}")
        return Result(domain, "skipped", reason)

    def process(self, url: str, source: str, name_hint: Optional[str] = None, force: bool = False) -> Result:
        trace = Trace(domain=url, source=source)
        result = self._process(url, source, name_hint, force, trace)
        if result.outcome != "duplicate":  # duplicates cost nothing and did no work
            trace.domain, trace.outcome, trace.detail = result.domain, result.outcome, result.detail
            self.traces.append(trace)
            if not self.dry_run:
                self.db.record_trace(self.run_id, trace)
        return result

    def _process(self, url: str, source: str, name_hint: Optional[str], force: bool, trace: Trace) -> Result:
        try:
            url = normalize_url(url)
        except ValueError as exc:
            return Result(url, "error", str(exc))
        domain = domain_of(url)

        status = self.db.status_of(domain)
        if status == "drafted" or (status == "skipped" and not force):
            return Result(domain, "duplicate", f"already {status}")
        if kind := non_company_kind(urlparse(url).hostname or ""):  # free: no scrape, no LLM call
            return self._skip(domain, url, source, _not_company_site(kind))
        facts = None
        if self.settings.max_employees and self.company_facts.enabled:  # only with an Apollo key
            with trace.timed("size"):
                facts = self.company_facts.lookup(domain)
        if facts and facts.employees and facts.employees > self.settings.max_employees:
            return self._skip(domain, url, source,
                              f"too big: ~{facts.employees} employees (Apollo), over MAX_EMPLOYEES={self.settings.max_employees}")
        size_note = f"; {facts.summary()}" if facts and facts.summary() else ""

        try:
            with trace.timed("scrape"):
                page = self.scraper.fetch(url)
        except ScrapeError as exc:
            return self._skip(domain, url, source, f"scrape failed: {exc}")
        if kind := non_company_kind(urlparse(page.url).hostname or ""):  # redirected to a job board
            return self._skip(domain, url, source, _not_company_site(kind))
        trace.pages, trace.text_chars, trace.truncated = len(page.pages), len(page.text), page.truncated

        try:
            with trace.timed("extract"):
                company = self.extractor.extract(page, name_hint)
        except ExtractionError as exc:  # not recorded: an API outage must not blacklist a company
            self._record("ERROR", domain, f"extraction failed: {exc}")
            return Result(domain, "error", f"extraction failed: {exc}")
        finally:
            usage = self.extractor.usage
            trace.model, trace.llm_calls = self.extractor.model, usage.calls
            trace.input_tokens, trace.output_tokens = usage.input_tokens, usage.output_tokens
        if company.page_type != "company":  # an article or posting about them, not their site
            return self._skip(domain, url, source, _not_company_site(company.page_type, company.company_name),
                              company_name=company.company_name)
        trace.founder_in_text = name_in_text(company.founder_first_name, company.founder_last_name, page.text)
        if trace.founder_in_text is False:  # named from memory, not the page: greet the team instead
            company = company.model_copy(update={"founder_first_name": None, "founder_last_name": None})

        founder = " ".join(p for p in (company.founder_first_name, company.founder_last_name) if p) or None

        # an open internship matters most when there's no good address to write to, so read the job
        # board before the email checks: skipped companies keep their apply link too
        role = apply_url = role_posted = None
        if page.job_boards and self.settings.check_job_boards:
            with trace.timed("jobs"):
                boards = self.boards.check(page.job_boards)
            max_age = self.settings.max_role_age_days
            best = boards.best_role(max_age)
            if best:
                role, apply_url, role_posted = best.title, best.url or None, best.posted
            engineering = boards.engineering_roles()
            stale = len(engineering) - len(boards.recent(engineering, max_age))
            if stale:
                self._record("INFO", domain, f"{stale} internship(s) posted over {max_age} days ago not named")
            if boards.problems:
                self._record("INFO", domain, f"job boards not read: {', '.join(boards.problems)}")
        about = dict(company=company.company_name, founder=founder, role=role, apply_url=apply_url,
                     role_posted=role_posted, size=facts.summary() if facts else "")

        def skip(reason: str) -> Result:
            result = self._skip(domain, url, source, reason, company_name=company.company_name, founder_name=founder)
            return replace(result, **about)

        with trace.timed("email"):
            email = self.finder.find(
                page.domain, company.company_name, company.founder_first_name,
                company.founder_last_name, page.mailtos,
            )
        if email is None:
            return skip("no founder email found")
        trace.email_source = f"{email.source} ({email.confidence})"
        if email.is_guess and not self.settings.allow_guessed_emails:
            return skip(f"{NEEDS_CONTACT}: guessed {email.email}, not verified")
        if self._generic_only(email, founder):
            reach = f"; reach {founder} directly" if founder else ""
            return skip(f"{NEEDS_CONTACT}: only a generic inbox ({email.email}){reach}")
        if self.db.email_taken(email.email):
            return skip(f"email {email.email} already drafted")

        posted_note = f", posted {role_posted}" if role and role_posted else ""
        role_note = (f"; role: {role}" if role else "") + posted_note + size_note
        draft = generate_email(company, self.profile, role=role)
        if self.dry_run:
            return Result(domain, "drafted", f"dry run ({email.source}: {email.confidence}){role_note}",
                          draft, email.email, **about)

        try:
            with trace.timed("draft"):
                draft_id = self.gmail.create_draft(email.email, draft.subject, draft.body)
        except GmailError as exc:
            self._record("ERROR", domain, f"gmail draft failed: {exc}")
            return Result(domain, "error", f"gmail draft failed: {exc}")

        self.db.mark_drafted(
            domain, url, source, company_name=company.company_name, founder_name=founder,
            founder_email=email.email, email_source=f"{email.source} ({email.confidence})", draft_id=draft_id,
        )
        self._record("INFO", domain, f"drafted to {email.email} via {email.source}{role_note}")
        return Result(domain, "drafted", f"{email.source}: {email.confidence}{role_note}", draft, email.email,
                      **about)

    def run(self, limit: int, source: str = "all", on_result: Optional[Reporter] = None) -> list[Result]:
        """Process queued targets first, then fresh leads, until `limit` drafts exist."""
        results: list[Result] = []
        drafted = 0
        attempts, max_attempts = 0, limit * 6  # bound LLM spend on unproductive leads

        def handle(url: str, src: str, hint: Optional[str]) -> None:
            nonlocal drafted, attempts
            attempts += 1
            result = self.process(url, src, hint)
            if result.outcome == "duplicate":
                attempts -= 1  # free: no network or LLM cost
                return
            results.append(result)
            if on_result:
                on_result(result)
            drafted += result.outcome == "drafted"

        for row in self.db.pending_queue(limit * 3):
            if drafted >= limit or attempts >= max_attempts:
                return results
            handle(row["url"], row["source"], None)

        seen: set[str] = set()  # dry runs record nothing, so also dedupe within this run
        streams = [p.iter_leads() for p in build_providers(self.settings, source)]
        prefer = parse_preferences(self.settings.prefer_regions)
        leads = (ranked_interleave(streams, lambda lead: region_rank(lead.regions, prefer))
                 if prefer else interleave(streams))
        for lead in leads:
            if drafted >= limit or attempts >= max_attempts:
                return results
            try:
                domain = domain_of(lead.url)
            except ValueError:
                continue
            if domain in seen or self.db.is_known(domain):
                continue
            seen.add(domain)
            handle(lead.url, lead.source, lead.name_hint)
        return results
