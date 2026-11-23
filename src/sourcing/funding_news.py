"""Source leads from startup funding news feeds: Inc42, Entrackr, and TechCrunch.

Startups that just raised a seed or early round are actively hiring. This source reads
funding articles from RSS feeds, checks robots.txt, and extracts the startup's own website.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
from bs4 import BeautifulSoup

from src.config import Settings
from src.sourcing.base import BaseSourcingProvider, Lead
from src.utils import is_company_host, is_investor_host, normalize_url, registered_domain, strip_markdown

log = logging.getLogger(__name__)

# (feed_url, regions)
FEEDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("https://inc42.com/feed/", ("India",)),
    ("https://entrackr.com/feed/", ("India",)),
    ("https://techcrunch.com/category/startups/feed/", ()),
)
_TRACKING_PREFIXES = ("utm_", "ref")


def _strip_tracking(url: str) -> str:
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.startswith(_TRACKING_PREFIXES)]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


class RobotChecker:
    """Cache and evaluate robots.txt rules for origin hosts."""

    def __init__(self, client: httpx.Client, user_agent: str) -> None:
        self.client = client
        self.user_agent = user_agent
        self._parsers: dict[str, RobotFileParser] = {}

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._parsers:
            rp = RobotFileParser()
            try:
                resp = self.client.get(f"{origin}/robots.txt")
                if resp.status_code in (401, 403):
                    rp.parse(["User-agent: *", "Disallow: /"])
                elif resp.status_code < 400:
                    rp.parse(resp.text.splitlines())
                else:
                    rp.parse([])
            except httpx.HTTPError:
                rp.parse([])
            self._parsers[origin] = rp
        return self._parsers[origin].can_fetch(self.user_agent, url)


def extract_company_url(html: str, article_url: str) -> Optional[str]:
    """Take the first outbound link in the article that is a real company site (not news/social/investor)."""
    soup = BeautifulSoup(html, "html.parser")
    article_host = registered_domain(urlparse(article_url).hostname or "")

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        target = urljoin(article_url, href).split("#")[0]
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https"):
            continue
        host = parsed.hostname or ""
        if not host or "." not in host:
            continue
        # Skip internal links on the same news domain
        if registered_domain(host) == article_host:
            continue
        # Skip if host is not a company or is an investor
        if not is_company_host(host) or is_investor_host(host):
            continue
        try:
            return normalize_url(_strip_tracking(target))
        except ValueError:
            continue
    return None


class FundingNewsProvider(BaseSourcingProvider):
    name = "funding"

    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept-Language": "en-US,en;q=0.9"},
        )
        self.robots = RobotChecker(self.client, settings.user_agent)

    def iter_leads(self) -> Iterator[Lead]:
        seen: set[str] = set()
        for feed_url, regions in FEEDS:
            try:
                resp = self.client.get(feed_url)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Funding news feed unavailable (%s): %s", feed_url, exc)
                continue

            for entry in feed.entries:
                article_url = (entry.get("link") or "").strip()
                if not article_url:
                    continue

                if not self.robots.is_allowed(article_url):
                    log.debug("robots.txt disallows %s; skipping", article_url)
                    continue

                try:
                    art_resp = self.client.get(article_url)
                    art_resp.raise_for_status()
                except httpx.HTTPError as exc:
                    log.debug("Could not fetch article %s: %s", article_url, exc)
                    continue

                company_url = extract_company_url(art_resp.text, str(art_resp.url))
                if not company_url:
                    continue
                if company_url in seen:
                    continue
                seen.add(company_url)

                title = strip_markdown(entry.get("title") or "")
                summary = BeautifulSoup(entry.get("summary") or "", "html.parser").get_text(" ")
                context = f"{title}. {summary}".strip()[:300]
                yield Lead(
                    url=company_url,
                    source=self.name,
                    name_hint=None,
                    context=context or None,
                    regions=regions,
                )
