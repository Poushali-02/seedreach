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


def strip_markdown(text: str) -> str:
    """Remove characters that would render as markdown in a plain-text email."""
    text = re.sub(r"[*#`]+", "", text)
    return re.sub(r"\s+", " ", text).strip()
