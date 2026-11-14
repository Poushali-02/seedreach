"""Source leads from Y Combinator companies that are hiring.

Uses the community-maintained yc-oss mirror of the public YC company directory
(https://github.com/yc-oss/api), which is plain JSON: no scraping, no key. It already
flags which companies are hiring, plus their team size, batch and regions.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Iterator, Optional

import httpx

from src.config import Settings
from src.sourcing.base import BaseSourcingProvider, Lead, parse_preferences, region_rank
from src.utils import strip_markdown

log = logging.getLogger(__name__)

API = "https://yc-oss.github.io/api/companies/hiring.json"


def _is_remote_friendly(company: dict[str, Any]) -> bool:
    return any("remote" in str(r).lower() for r in company.get("regions") or [])


def parse_regions(raw: str) -> set[str]:
    """`"India, Remote"` -> {"india", "remote"}. Empty entries are ignored."""
    return {r.strip().lower() for r in raw.split(",") if r.strip()}


def _region_names(company: dict[str, Any]) -> set[str]:
    return {str(r).strip().lower() for r in company.get("regions") or []}


def select_companies(
    companies: list[dict[str, Any]], max_team_size: int, regions: Optional[set[str]] = None,
    prefer: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Active, hiring, small companies with a website: remote-friendly first, then newest launches.

    With `regions`, keep only companies that list at least one of them. Names are matched whole
    (case-insensitive), so "remote" doesn't also match YC's separate "Partly Remote" region.
    With `prefer` (see parse_preferences), companies listing the first preferred region come first,
    then the second, and so on, then the rest; the usual order applies within each group.
    """
    picked = []
    for c in companies:
        if c.get("status") != "Active" or not c.get("isHiring") or not c.get("website"):
            continue
        if regions and not regions & _region_names(c):
            continue
        size = c.get("team_size")
        if isinstance(size, int):
            if size > max_team_size:
                continue
        elif c.get("stage") != "Early":  # unknown size: only keep if YC marks it early-stage
            continue
        picked.append(c)
    picked.sort(key=lambda c: (region_rank(c.get("regions") or [], prefer),
                               not _is_remote_friendly(c), -(c.get("launched_at") or 0)))
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

        regions = parse_regions(self.settings.yc_regions)
        prefer = parse_preferences(self.settings.prefer_regions)
        known = {name for c in companies for name in _region_names(c)}
        for setting, names in (("YC_REGIONS", regions), ("PREFER_REGIONS", set(prefer))):
            unknown = names - known
            if unknown:  # most likely a typo: YC's names are e.g. "India", "Remote", "America / Canada"
                log.warning("%s: no hiring YC company lists %s; check the spelling",
                            setting, ", ".join(sorted(unknown)))
        for c in select_companies(companies, self.settings.yc_max_team_size, regions, prefer):
            batch = c.get("batch") or "YC"
            yield Lead(
                url=c["website"],
                source=self.name,
                name_hint=strip_markdown(c.get("name") or "")[:80] or None,
                context=strip_markdown(f"{batch}: {c.get('one_liner') or ''}")[:300],
                regions=tuple(str(r) for r in c.get("regions") or []),
                founders_url=c.get("url") or None,
            )


def yc_founders(page_url: str, client: httpx.Client) -> list[tuple[str, str]]:
    """[(name, title)] from a company's YC page, CEO first; [] if the page can't be read. Never raises.

    YC pages embed their data as JSON in a `data-page` attribute; the founders are under props.company.
    """
    try:
        resp = client.get(page_url)
        resp.raise_for_status()
        match = re.search(r'data-page="([^"]*)"', resp.text)
        props = json.loads(html.unescape(match.group(1)))["props"] if match else {}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.debug("YC founders not read from %s: %s", page_url, exc)
        return []
    founders = []
    for f in ((props.get("company") or {}).get("founders") or []):
        name = strip_markdown(str(f.get("full_name") or "")).strip()
        if name:
            founders.append((name, str(f.get("title") or "")))
    return sorted(founders, key=lambda f: "ceo" not in f[1].lower())
