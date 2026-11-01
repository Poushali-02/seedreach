"""Fetch a startup's landing page (plus about/team pages) and reduce it to clean text."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from src.config import Settings
from src.utils import domain_of, normalize_url, registered_domain

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 16_000
MAX_EXTRA_PAGES = 3
EXTRA_PAGE_CHARS = 3_000  # each extra page keeps this much, so a long first page can't push it out
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Same-site pages likely to name the founders, best first, matched on the link's path and text.
# Contact pages rarely name anyone but often list an email, so they still get a slot when one is free.
_LINK_TIERS = tuple(re.compile(rf"\b({words})\b", re.I) for words in (  # whole words: "teams" is not "team"
    r"founders?|co-?founders?|team|people|leadership|who[- ]we[- ]are|über uns|ueber-uns|uber-uns|about[- ]us|our[- ]story",
    r"about|company|story|vision|mission",
    r"contact",
))
_SKIP_LINK = re.compile(r"career|jobs|blog/|news|press|privacy|terms|legal", re.I)
_FOUNDER_WORD = re.compile(r"co-?founder|founder|gründer", re.I)


def _excerpt(text: str, limit: int) -> str:
    """Up to `limit` characters, starting a little before the first mention of a founder if it's further in."""
    match = _FOUNDER_WORD.search(text)
    start = 0 if match is None or match.end() <= limit else max(0, match.start() - limit // 3)
    return text[start:start + limit]
_NOISE_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "template")
_FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")


class ScrapeError(Exception):
    """The page could not be fetched or contained no usable HTML."""


@dataclass
class ScrapedPage:
    url: str
    domain: str
    title: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    text: str = ""
    mailtos: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)  # every URL whose text is included
    truncated: bool = False  # text was cut to MAX_TEXT_CHARS

    def as_prompt_text(self) -> str:
        meta = "\n".join(f"{k}: {v}" for k, v in self.meta.items() if v)
        return f"URL: {self.url}\nTitle: {self.title}\n{meta}\n\n{self.text}"


def _extract_emails(candidates: list[str]) -> list[str]:
    out: list[str] = []
    for raw in candidates:
        email = raw.lower().strip().rstrip(".")
        if email.endswith(_FILE_SUFFIXES) or email in out:
            continue
        out.append(email)
    return out


def _parse(html: str, url: str) -> tuple[ScrapedPage, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    page = ScrapedPage(url=url, domain=domain_of(url))
    page.title = soup.title.get_text(strip=True) if soup.title else ""

    for tag in soup.find_all("meta"):
        key = tag.get("name") or tag.get("property")
        if key in {"description", "og:title", "og:description", "og:site_name", "twitter:description"}:
            page.meta[key] = (tag.get("content") or "").strip()

    mailtos = [
        a["href"][7:].split("?")[0] for a in soup.find_all("a", href=True)
        if a["href"].lower().startswith("mailto:")
    ]
    # candidate team/about links, same site only, ranked by how likely they are to name the founders
    host = registered_domain(urlparse(url).hostname or "")
    ranked: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        target = urljoin(url, a["href"]).split("#")[0]
        t = urlparse(target)
        if t.scheme not in ("http", "https") or registered_domain(t.hostname or "") != host:
            continue
        if target.rstrip("/") == url.rstrip("/") or len([s for s in t.path.split("/") if s]) > 2:
            continue  # the page itself, or a deep page such as a blog post
        if _SKIP_LINK.search(t.path):
            continue
        label = f"{t.path} {a.get_text(' ', strip=True)}"
        tier = next((i for i, rx in enumerate(_LINK_TIERS) if rx.search(label)), None)
        if tier is not None and tier < ranked.get(target, len(_LINK_TIERS)):
            ranked[target] = tier
    extra_links = sorted(ranked, key=ranked.__getitem__)  # stable: page order within a tier

    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    text = soup.get_text("\n")
    page.mailtos = _extract_emails(mailtos + _EMAIL_RE.findall(text))
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines())
    page.text = "\n".join(ln for ln in lines if ln)
    return page, extra_links


class Scraper:
    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None) -> None:
        self.client = client or httpx.Client(
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def _get_html(self, url: str) -> tuple[str, str]:
        last: Optional[Exception] = None
        for _attempt in range(2):
            try:
                resp = self.client.get(url)
                resp.raise_for_status()
                if "html" not in resp.headers.get("content-type", "html").lower():
                    raise ScrapeError(f"{url} is not an HTML page")
                return resp.text, str(resp.url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:  # 4xx will not improve on retry
                    raise ScrapeError(f"HTTP {exc.response.status_code} for {url}") from exc
                last = exc
            except httpx.HTTPError as exc:
                last = exc
        raise ScrapeError(f"Could not fetch {url}: {last}")

    def fetch(self, url: str) -> ScrapedPage:
        url = normalize_url(url)
        html, final_url = self._get_html(url)
        page, extra_links = _parse(html, final_url)
        page.pages = [final_url]

        extras: list[str] = []
        for link in extra_links[:MAX_EXTRA_PAGES]:
            try:
                sub_html, sub_url = self._get_html(link)
            except ScrapeError as exc:
                log.debug("skipping extra page: %s", exc)
                continue
            sub, _ = _parse(sub_html, sub_url)
            page.truncated |= len(sub.text) > EXTRA_PAGE_CHARS
            extras.append(f"\n\n--- {sub_url} ---\n{_excerpt(sub.text, EXTRA_PAGE_CHARS)}")
            page.mailtos = _extract_emails(page.mailtos + sub.mailtos)
            page.pages.append(sub_url)

        # the first page gets what the extra pages leave, so the team page always reaches the model
        budget = MAX_TEXT_CHARS - sum(len(e) for e in extras)
        page.truncated |= len(page.text) > budget
        page.text = page.text[:budget] + "".join(extras)
        if len(page.text) < 80 and not page.meta:
            raise ScrapeError(f"{url} has too little readable text (JS-only site?)")
        return page
