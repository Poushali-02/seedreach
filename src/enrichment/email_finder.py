"""Find a founder's email: Apollo -> homepage mailto -> {first}@domain + MX check."""

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


@dataclass(frozen=True)
class EmailResult:
    email: str
    source: Source
    confidence: str  # human-readable: verified / likely / generic / guess


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

    # ---- providers -----------------------------------------------------
    def _apollo(self, domain: str, company: str, first: str, last: Optional[str]) -> Optional[EmailResult]:
        if not self.settings.apollo_api_key:
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
    def _from_mailtos(domain: str, mailtos: list[str], first: Optional[str]) -> Optional[EmailResult]:
        usable = [m for m in mailtos if m.endswith("@" + domain) or m.endswith("." + domain)]
        usable = [m for m in usable if m.split("@")[0] not in _BAD_LOCALPARTS]
        if first:
            f = _slug(first)
            for m in usable:
                if f and f in _slug(m.split("@")[0]):
                    return EmailResult(m, "mailto", "likely")
        personal = [m for m in usable if m.split("@")[0] not in _GENERIC_OK]
        if personal:
            return EmailResult(personal[0], "mailto", "likely")
        generic = [m for m in usable if m.split("@")[0] in _GENERIC_OK]
        if generic:
            return EmailResult(generic[0], "mailto", "generic")
        return None

    @staticmethod
    def _heuristic(domain: str, first: Optional[str]) -> Optional[EmailResult]:
        slug = _slug(first or "")
        if not slug or not has_mx(domain):
            return None
        return EmailResult(f"{slug}@{domain}", "heuristic", "guess (MX ok, mailbox unverified)")

    # ---- public --------------------------------------------------------
    def find(
        self, domain: str, company: str, first: Optional[str], last: Optional[str], mailtos: list[str],
    ) -> Optional[EmailResult]:
        if first:
            if result := self._apollo(domain, company, first, last):
                return result
        if result := self._from_mailtos(domain, mailtos, first):
            return result
        return self._heuristic(domain, first)
