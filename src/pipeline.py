"""Orchestrates scrape -> extract -> enrich -> generate -> Gmail draft, with dedupe."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from src.config import Settings
from src.db import Database
from src.enrichment.email_finder import EmailFinder
from src.intelligence.extractor import ExtractionError, Extractor
from src.intelligence.scraper import ScrapeError, Scraper
from src.outreach.generator import EmailDraft, generate_email
from src.outreach.gmail_client import GmailDraftClient, GmailError
from src.profile import load_profile
from src.sourcing.base import BaseSourcingProvider, Lead
from src.sourcing.hacker_news import HackerNewsProvider
from src.sourcing.product_hunt import ProductHuntProvider
from src.utils import domain_of, normalize_url

log = logging.getLogger(__name__)

# Outcomes: drafted | skipped (recorded, never retried) | duplicate (already known) | error (not recorded, retryable)
Reporter = Callable[["Result"], None]


@dataclass
class Result:
    domain: str
    outcome: str
    detail: str
    draft: Optional[EmailDraft] = None
    to: Optional[str] = None


def build_providers(settings: Settings, names: str = "all") -> list[BaseSourcingProvider]:
    available: dict[str, Callable[[Settings], BaseSourcingProvider]] = {
        "hn": HackerNewsProvider,
        "producthunt": ProductHuntProvider,
    }
    if names == "all":
        return [factory(settings) for factory in available.values()]
    if names not in available:
        raise ValueError(f"Unknown source {names!r}; choose from: all, {', '.join(available)}")
    return [available[names](settings)]


class Pipeline:
    def __init__(self, settings: Settings, db: Database, *, dry_run: bool = False) -> None:
        self.settings = settings
        self.db = db
        self.dry_run = dry_run
        self.profile = load_profile()
        self.scraper = Scraper(settings)
        self.extractor = Extractor(settings)
        self.finder = EmailFinder(settings)
        self.gmail = GmailDraftClient(settings.google_credentials_file, settings.google_token_file, settings.headless)
        if not dry_run:
            self.gmail.ensure_authenticated()  # fail before spending LLM credits

    def _record(self, level: str, domain: str, message: str) -> None:
        # the CLI already prints a status line per company, so routine events only go to debug output
        log.log(logging.DEBUG if level == "INFO" else getattr(logging, level), "%s: %s", domain, message)
        if not self.dry_run:
            self.db.log(level, message, domain)

    def _skip(self, domain: str, url: str, source: str, reason: str, **kw: Optional[str]) -> Result:
        if not self.dry_run:
            self.db.mark_skipped(domain, url, source, reason, **kw)
        self._record("INFO", domain, f"skipped: {reason}")
        return Result(domain, "skipped", reason)

    def process(self, url: str, source: str, name_hint: Optional[str] = None, force: bool = False) -> Result:
        try:
            url = normalize_url(url)
        except ValueError as exc:
            return Result(url, "error", str(exc))
        domain = domain_of(url)

        status = self.db.status_of(domain)
        if status == "drafted" or (status == "skipped" and not force):
            return Result(domain, "duplicate", f"already {status}")

        try:
            page = self.scraper.fetch(url)
        except ScrapeError as exc:
            return self._skip(domain, url, source, f"scrape failed: {exc}")

        try:
            company = self.extractor.extract(page, name_hint)
        except ExtractionError as exc:  # not recorded: an API outage must not blacklist a company
            self._record("ERROR", domain, f"extraction failed: {exc}")
            return Result(domain, "error", f"extraction failed: {exc}")

        founder = " ".join(p for p in (company.founder_first_name, company.founder_last_name) if p) or None
        email = self.finder.find(
            page.domain, company.company_name, company.founder_first_name,
            company.founder_last_name, page.mailtos,
        )
        if email is None:
            return self._skip(domain, url, source, "no founder email found",
                              company_name=company.company_name, founder_name=founder)
        if self.db.email_taken(email.email):
            return self._skip(domain, url, source, f"email {email.email} already drafted",
                              company_name=company.company_name, founder_name=founder)

        draft = generate_email(company, self.profile)
        if self.dry_run:
            return Result(domain, "drafted", f"dry run ({email.source}: {email.confidence})", draft, email.email)

        try:
            draft_id = self.gmail.create_draft(email.email, draft.subject, draft.body)
        except GmailError as exc:
            self._record("ERROR", domain, f"gmail draft failed: {exc}")
            return Result(domain, "error", f"gmail draft failed: {exc}")

        self.db.mark_drafted(
            domain, url, source, company_name=company.company_name, founder_name=founder,
            founder_email=email.email, email_source=f"{email.source} ({email.confidence})", draft_id=draft_id,
        )
        self._record("INFO", domain, f"drafted to {email.email} via {email.source}")
        return Result(domain, "drafted", f"{email.source}: {email.confidence}", draft, email.email)

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

        for provider in build_providers(self.settings, source):
            for lead in provider.fetch(limit * 8):
                if drafted >= limit or attempts >= max_attempts:
                    return results
                try:
                    if self.db.is_known(domain_of(lead.url)):
                        continue
                except ValueError:
                    continue
                handle(lead.url, lead.source, lead.name_hint)
        return results
