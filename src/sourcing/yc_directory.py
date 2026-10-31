"""Source leads from Y Combinator companies that are hiring.

Uses the community-maintained yc-oss mirror of the public YC company directory
(https://github.com/yc-oss/api), which is plain JSON: no scraping, no key. It already
flags which companies are hiring, plus their team size, batch and regions.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

import httpx

from src.config import Settings
from src.sourcing.base import BaseSourcingProvider, Lead
from src.utils import strip_markdown

log = logging.getLogger(__name__)

API = "https://yc-oss.github.io/api/companies/hiring.json"


def _is_remote_friendly(company: dict[str, Any]) -> bool:
    return any("remote" in str(r).lower() for r in company.get("regions") or [])


def select_companies(companies: list[dict[str, Any]], max_team_size: int) -> list[dict[str, Any]]:
    """Active, hiring, small companies with a website: remote-friendly first, then newest launches."""
    picked = []
    for c in companies:
        if c.get("status") != "Active" or not c.get("isHiring") or not c.get("website"):
            continue
        size = c.get("team_size")
        if isinstance(size, int):
            if size > max_team_size:
                continue
        elif c.get("stage") != "Early":  # unknown size: only keep if YC marks it early-stage
            continue
        picked.append(c)
    picked.sort(key=lambda c: (not _is_remote_friendly(c), -(c.get("launched_at") or 0)))
    return picked


class YCDirectoryProvider(BaseSourcingProvider):
    name = "yc"

    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=max(settings.http_timeout, 30.0))

    def iter_leads(self) -> Iterator[Lead]:
        try:
            resp = self.client.get(API)
            resp.raise_for_status()
            companies = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("YC directory unavailable: %s", exc)
            return

        for c in select_companies(companies, self.settings.yc_max_team_size):
            batch = c.get("batch") or "YC"
            yield Lead(
                url=c["website"],
                source=self.name,
                name_hint=strip_markdown(c.get("name") or "")[:80] or None,
                context=strip_markdown(f"{batch}: {c.get('one_liner') or ''}")[:300],
            )
