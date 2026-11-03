"""Small shared helpers: URL/domain normalisation and text cleaning."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Hosts where the subdomain *is* the company, so we keep the full hostname.
_PLATFORM_SUFFIXES = (
    "vercel.app", "netlify.app", "github.io", "herokuapp.com", "pages.dev",
    "web.app", "notion.site", "carrd.co", "framer.website", "webflow.io",
    "replit.app", "onrender.com", "fly.dev", "railway.app", "streamlit.app",
)
# Second-level labels used under two-letter ccTLDs (foo.co.uk, foo.com.au).
_CC_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}


# A page on one of these hosts is not the company's own site: job boards, news, socials, docs, forms.
JOB_BOARD_HOSTS = (
    "lever.co", "greenhouse.io", "ashbyhq.com", "workable.com", "bamboohr.com", "smartrecruiters.com",
    "recruitee.com", "breezy.hr", "myworkdayjobs.com", "workatastartup.com", "jobs.gem.com", "rippling.com",
    "teamtailor.com", "factorialhr.com", "personio.com", "personio.de", "join.com", "wellfound.com",
    "angel.co", "ycombinator.com", "linkedin.com", "lnkd.in", "naukri.com", "internshala.com", "grnh.se",
)
NEWS_HOSTS = (
    "techcrunch.com", "mercurynews.com", "forbes.com", "businessinsider.com", "theverge.com", "wired.com",
    "venturebeat.com", "bloomberg.com", "reuters.com", "yourstory.com", "inc42.com", "economictimes.com",
    "medium.com", "substack.com",
)
_OTHER_NON_COMPANY = (
    "github.com", "twitter.com", "x.com", "notion.site", "notion.so", "docs.google.com", "drive.google.com",
    "calendly.com", "forms.gle", "typeform.com", "youtube.com", "t.me", "discord.gg",
)


def non_company_kind(host: str) -> str | None:
    """'job_board', 'news' or 'other' if a page on this host can't be the company's own site, else None."""
    host = host.lower().rstrip(".")

    def on(suffixes: tuple[str, ...]) -> bool:
        return any(host == s or host.endswith("." + s) for s in suffixes)

    if on(JOB_BOARD_HOSTS) or host.startswith(("jobs.", "careers.")):
        return "job_board"
    if on(NEWS_HOSTS):
        return "news"
    return "other" if on(_OTHER_NON_COMPANY) else None


def is_company_host(host: str) -> bool:
    return bool(host) and non_company_kind(host) is None


def normalize_url(raw: str) -> str:
    """Return an absolute http(s) URL, adding https:// when the scheme is missing."""
    raw = raw.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or "." not in parsed.hostname:
        raise ValueError(f"Not a valid website URL: {raw!r}")
    return raw


def registered_domain(host: str) -> str:
    """Best-effort registrable domain (no public-suffix list needed for our purposes)."""
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if host.endswith(_PLATFORM_SUFFIXES):
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if len(labels[-1]) == 2 and labels[-2] in _CC_SECOND_LEVEL and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def domain_of(url: str) -> str:
    host = urlparse(normalize_url(url)).hostname or ""
    return registered_domain(host)


_LEGAL_SUFFIX = re.compile(
    r"[,\s]+(inc|llc|ltd|limited|gmbh|oy|ab|corp|corporation|co|pvt\.?\s*ltd|private limited|s\.?a\.?s?|b\.?v)\.?$",
    re.I,
)


def display_name(company: str) -> str:
    """The company name as people say it: 'Vernius Systems, Inc.' -> 'Vernius Systems'."""
    name = company.strip()
    while (stripped := _LEGAL_SUFFIX.sub("", name)) != name and stripped:
        name = stripped
    return name


def strip_markdown(text: str) -> str:
    """Remove characters that would render as markdown in a plain-text email."""
    text = re.sub(r"[*#`]+", "", text)
    return re.sub(r"\s+", " ", text).strip()
