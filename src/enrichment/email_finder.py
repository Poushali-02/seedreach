"""Find a founder's email: Apollo -> addresses on the site -> a guess in the site's address format.

Every result carries a confidence:
  verified           Apollo says the mailbox exists
  on site            a personal address printed on the company's pages
  generic            hello@ / founders@ / team@ printed on the site
  pattern from site  the founder's name in the format the site's own addresses use (first.last@, first@)
  guess              {first}@domain with no evidence beyond an MX record; the pipeline does not draft these
                     unless ALLOW_GUESSED_EMAILS is set
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Optional

import dns.exception
import dns.resolver
import httpx

from src.config import Settings

log = logging.getLogger(__name__)

APOLLO_URL = "https://api.apollo.io/v1/people/match"

# Inboxes nobody reads as a person (or that would be rude to cold-email).
_BAD_LOCALPARTS = {
    "support", "press", "jobs", "careers", "privacy", "legal", "abuse", "noreply", "no-reply",
    "security", "billing", "sales", "help", "admin", "webmaster", "postmaster", "media", "pr",
}
_GENERIC_OK = {"founders", "founder", "hello", "hi", "team", "contact", "info", "hey"}

Source = Literal["apollo", "mailto", "heuristic"]
GUESS = "guess"


@dataclass(frozen=True)
class EmailResult:
    email: str
    source: Source
    confidence: str  # verified / on site / generic / pattern from site / guess (see module docstring)

    @property
    def is_guess(self) -> bool:
        return self.confidence == GUESS


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_name.lower())


def has_mx(domain: str, timeout: float = 5.0) -> bool:
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        return len(resolver.resolve(domain, "MX")) > 0
    except (dns.exception.DNSException, OSError):
        return False


class EmailFinder:
    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout)
        self.apollo_enabled = bool(settings.apollo_api_key)  # off for the run once Apollo refuses the key

    # ---- providers -----------------------------------------------------
    def _apollo(self, domain: str, company: str, first: str, last: Optional[str]) -> Optional[EmailResult]:
        if not self.apollo_enabled:
            return None
        payload = {
            "first_name": first, "last_name": last or "", "domain": domain,
            "organization_name": company, "reveal_personal_emails": False,
        }
        try:
            resp = self.client.post(
                APOLLO_URL, json=payload,
                headers={"X-Api-Key": self.settings.apollo_api_key, "Cache-Control": "no-cache"},
            )
            if resp.status_code in (401, 403):  # bad key, or the free plan (people/match is paid-only)
                self.apollo_enabled = False
                log.warning("Apollo email lookups are off for this run (HTTP %s): %s",
                            resp.status_code, resp.text[:160])
                return None
            resp.raise_for_status()
            person = resp.json().get("person") or {}
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Apollo lookup failed: %s", exc)
            return None
        email = (person.get("email") or "").lower()
        if not email or "email_not_unlocked" in email or "@" not in email:
            return None
        status = person.get("email_status") or "unknown"
        return EmailResult(email, "apollo", "verified" if status == "verified" else f"apollo:{status}")

    @staticmethod
    def _site_addresses(domain: str, mailtos: list[str]) -> tuple[list[str], list[str]]:
        """(personal, generic) addresses at the company's domain, minus inboxes nobody reads as a person."""
        usable = [m for m in mailtos if m.endswith("@" + domain) or m.endswith("." + domain)]
        usable = [m for m in usable if m.split("@")[0] not in _BAD_LOCALPARTS]
        return ([m for m in usable if m.split("@")[0] not in _GENERIC_OK],
                [m for m in usable if m.split("@")[0] in _GENERIC_OK])

    @staticmethod
    def _site_pattern(personal: list[str]) -> Optional[str]:
        """The format the site's own personal addresses use: 'first.last', 'first_last' or 'first'."""
        local_parts = [m.split("@")[0] for m in personal]
        for sep in (".", "_"):
            if any(re.fullmatch(rf"[a-z]+\{sep}[a-z]+", p) for p in local_parts):
                return f"first{sep}last"
        return "first" if any(re.fullmatch(r"[a-z]{2,}", p) for p in local_parts) else None

    # ---- public --------------------------------------------------------
    def find(
        self, domain: str, company: str, first: Optional[str], last: Optional[str], mailtos: list[str],
    ) -> Optional[EmailResult]:
        """Best address in order: verified, the founder's own on the site, the founder's in the site's format,
        anyone's on the site, a generic inbox, then a bare guess."""
        f, l = _slug(first or ""), _slug(last or "")
        if first and (result := self._apollo(domain, company, first, last)):
            return result
        personal, generic = self._site_addresses(domain, mailtos)
        if f and (mine := next((m for m in personal + generic if f in _slug(m.split("@")[0])), None)):
            return EmailResult(mine, "mailto", "on site")
        pattern = self._site_pattern(personal)
        if f and pattern and (pattern == "first" or l) and has_mx(domain):
            local = f if pattern == "first" else f"{f}{pattern[5]}{l}"
            return EmailResult(f"{local}@{domain}", "heuristic", "pattern from site")
        if personal:
            return EmailResult(personal[0], "mailto", "on site")
        if generic:
            return EmailResult(generic[0], "mailto", "generic")
        if f and has_mx(domain):
            return EmailResult(f"{f}@{domain}", "heuristic", GUESS)
        return None
