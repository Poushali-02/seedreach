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

MAX_TEXT_CHARS = 12_000
MAX_EXTRA_PAGES = 2
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ABOUT_HINT = re.compile(r"/(about|team|company|founders?|people|contact)(/|$|\?)", re.I)
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
    # candidate about/team links, same site only
    host = registered_domain(urlparse(url).hostname or "")
    extra_links: list[str] = []
    for a in soup.find_all("a", href=True):
        target = urljoin(url, a["href"]).split("#")[0]
        t = urlparse(target)
        if t.scheme in ("http", "https") and registered_domain(t.hostname or "") == host \
                and _ABOUT_HINT.search(t.path + "/") and target not in extra_links and target != url:
            extra_links.append(target)

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

        for link in extra_links[:MAX_EXTRA_PAGES]:
            try:
                sub_html, sub_url = self._get_html(link)
            except ScrapeError as exc:
                log.debug("skipping extra page: %s", exc)
                continue
            sub, _ = _parse(sub_html, sub_url)
            page.text += f"\n\n--- {sub_url} ---\n{sub.text}"
            page.mailtos = _extract_emails(page.mailtos + sub.mailtos)

        page.text = page.text[:MAX_TEXT_CHARS]
        if len(page.text) < 80 and not page.meta:
            raise ScrapeError(f"{url} has too little readable text (JS-only site?)")
        return page
