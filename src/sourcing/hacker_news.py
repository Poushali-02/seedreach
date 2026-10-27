"""Source leads from the monthly 'Ask HN: Who is hiring?' threads via the Algolia API.

Note: the `whoishiring` account only posts the *threads*; the job posts are replies by
other users. So we first locate the latest threads, then search the comments under them.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.config import Settings
from src.sourcing.base import BaseSourcingProvider, Lead
from src.utils import strip_markdown

log = logging.getLogger(__name__)

API = "https://hn.algolia.com/api/v1/search_by_date"
SEARCH_QUERIES = ("remote python", "remote backend", "remote intern", "remote AI")

_ROLE_RE = re.compile(
    r"\b(back-?end|python|django|fastapi|intern(?:ship)?s?|forward[- ]deployed|systems? engineer)\b", re.I
)
_AI_RE = re.compile(r"\b(AI|ML|LLMs?)\b|\bagentic\b", re.I)  # AI/ML matched loosely but on word boundary
_REMOTE_RE = re.compile(r"\bremote\b", re.I)

# Job boards / socials / docs: a link here is not the company's own site.
_NON_COMPANY_SUFFIXES = (
    "ycombinator.com", "lever.co", "greenhouse.io", "ashbyhq.com", "workable.com", "linkedin.com",
    "github.com", "twitter.com", "x.com", "wellfound.com", "angel.co", "notion.site", "notion.so",
    "docs.google.com", "drive.google.com", "bamboohr.com", "smartrecruiters.com", "recruitee.com",
    "breezy.hr", "myworkdayjobs.com", "workatastartup.com", "jobs.gem.com", "rippling.com",
    "calendly.com", "forms.gle", "typeform.com", "medium.com", "youtube.com", "t.me", "discord.gg",
)


def _is_company_host(host: str) -> bool:
    host = host.lower()
    return bool(host) and not host.endswith(_NON_COMPANY_SUFFIXES) and not host.startswith(("jobs.", "careers."))


def parse_job_comment(html: str) -> Optional[Lead]:
    """Turn one HN job comment into a Lead (or None if it doesn't match our criteria)."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n").strip()
    if not text or not _REMOTE_RE.search(text):
        return None
    if not (_ROLE_RE.search(text) or _AI_RE.search(text)):
        return None

    url = None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        host = urlparse(href).hostname or ""
        if href.startswith("http") and _is_company_host(host):
            url = href
            break
    if not url:
        return None

    header = text.splitlines()[0]
    name = strip_markdown(header.split("|")[0])[:80] or None
    return Lead(url=url, source="hacker_news", name_hint=name, context=strip_markdown(text)[:500])


class HackerNewsProvider(BaseSourcingProvider):
    name = "hacker_news"

    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout)

    def _get(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            resp = self.client.get(API, params=params)
            resp.raise_for_status()
            return resp.json().get("hits", [])
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("HN Algolia request failed: %s", exc)
            return []

    def _latest_threads(self, count: int = 2) -> list[str]:
        hits = self._get({"tags": "story,author_whoishiring", "query": "Ask HN: Who is hiring?", "hitsPerPage": 10})
        return [
            h["objectID"] for h in hits
            if h.get("title", "").startswith("Ask HN: Who is hiring?")
        ][:count]

    def fetch(self, limit: int) -> list[Lead]:
        cutoff = int(time.time()) - self.settings.hn_lookback_days * 86400
        leads: list[Lead] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        for story_id in self._latest_threads():
            for query in SEARCH_QUERIES:
                hits = self._get({
                    "tags": f"comment,story_{story_id}",
                    "query": query,
                    "numericFilters": f"created_at_i>{cutoff}",
                    "hitsPerPage": 100,
                })
                for hit in hits:
                    oid = hit.get("objectID")
                    # top-level comments only: replies to job posts are not job posts
                    if oid in seen_ids or str(hit.get("parent_id")) != story_id:
                        continue
                    seen_ids.add(oid)
                    lead = parse_job_comment(hit.get("comment_text") or "")
                    if lead and lead.url not in seen_urls:
                        seen_urls.add(lead.url)
                        leads.append(lead)
                if len(leads) >= limit:
                    return leads[:limit]
        log.info("HN: %d leads matched", len(leads))
        return leads[:limit]
