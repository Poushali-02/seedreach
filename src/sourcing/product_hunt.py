"""Source leads from Product Hunt's public Atom feed (best effort; PH may block scrapers)."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from src.config import Settings
from src.sourcing.base import BaseSourcingProvider, Lead
from src.utils import strip_markdown

log = logging.getLogger(__name__)

FEED_URL = "https://www.producthunt.com/feed"
_TRACKING_PREFIXES = ("utm_", "ref")


def _strip_tracking(url: str) -> str:
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.startswith(_TRACKING_PREFIXES)]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


class ProductHuntProvider(BaseSourcingProvider):
    name = "product_hunt"

    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(
            timeout=settings.http_timeout, follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept-Language": "en-US,en;q=0.9"},
        )

    def _resolve_website(self, post_url: str) -> Optional[str]:
        """PH post pages link out via /r/... redirects; follow one to find the real site."""
        try:
            page = self.client.get(post_url)
            page.raise_for_status()
            soup = BeautifulSoup(page.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/r/" in href and "producthunt.com" in urlparse(page.url.join(href)).netloc:
                    final = self.client.get(page.url.join(href))
                    host = final.url.host or ""
                    if "producthunt.com" not in host:
                        return _strip_tracking(str(final.url))
        except httpx.HTTPError as exc:
            log.debug("PH resolve failed for %s: %s", post_url, exc)
        return None

    def fetch(self, limit: int) -> list[Lead]:
        try:
            resp = self.client.get(FEED_URL)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Product Hunt feed unavailable: %s", exc)
            return []

        leads: list[Lead] = []
        for entry in feedparser.parse(resp.content).entries:
            if len(leads) >= limit:
                break
            website = self._resolve_website(entry.get("link", ""))
            if not website:
                continue
            summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ")
            leads.append(Lead(
                url=website, source=self.name,
                name_hint=strip_markdown(entry.get("title", ""))[:80] or None,
                context=strip_markdown(summary)[:300],
            ))
        log.info("Product Hunt: %d leads resolved", len(leads))
        return leads
