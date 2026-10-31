"""Source leads from recent "Launch HN" posts: YC startups announcing their launch on Hacker News.

These are younger than most "Who is hiring?" companies, and the founders post them themselves.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

import httpx

from src.config import Settings
from src.sourcing.base import BaseSourcingProvider, Lead
from src.sourcing.hacker_news import _is_company_host
from src.utils import strip_markdown

log = logging.getLogger(__name__)

API = "https://hn.algolia.com/api/v1/search_by_date"
MAX_PAGES = 3
_NAME_RE = re.compile(r"^Launch HN:\s*(.+?)\s*\((?:YC|yc)\b")


def parse_launch(hit: dict[str, Any]) -> Optional[Lead]:
    """A Launch HN story becomes a lead only if it links to the company's own site."""
    url = (hit.get("url") or "").strip()
    host = urlparse(url).hostname or ""
    if not url.startswith("http") or not _is_company_host(host):
        return None  # text-only posts, or links to GitHub, YouTube and the like
    title = strip_markdown(hit.get("title") or "")
    match = _NAME_RE.match(title)
    return Lead(url=url, source="launchhn", name_hint=match[1] if match else None, context=title[:300])


class LaunchHNProvider(BaseSourcingProvider):
    name = "launchhn"

    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout)

    def iter_leads(self) -> Iterator[Lead]:
        cutoff = int(time.time()) - self.settings.launchhn_lookback_days * 86400
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            try:
                resp = self.client.get(API, params={
                    "tags": "launch_hn",
                    "numericFilters": f"created_at_i>{cutoff}",
                    "hitsPerPage": 100,
                    "page": page,
                })
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Launch HN request failed: %s", exc)
                return
            for hit in data.get("hits", []):
                lead = parse_launch(hit)
                if lead and lead.url not in seen:
                    seen.add(lead.url)
                    yield lead
            if page + 1 >= data.get("nbPages", 0):
                return
