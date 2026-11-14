"""Job-alert emails (#36): the openings a job portal emails you, read from your own Gmail inbox.

Portals such as Naukri forbid scraping, but they email you the roles that match the alerts you set up
there. With JOB_ALERTS=1, `run` reads those emails (read-only) and lists each new opening in the run
digest with its apply link. An alert names a company but not its website, so these openings are listed
for you to apply to; they don't become drafts. To also write to a company, add its site yourself with
`python main.py add-target <url>`.

One small parser per sender. An email from a sender with no parser is never opened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from src.enrichment.job_boards import _INTERN

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertJob:
    portal: str  # "Naukri"
    company: str
    role: str
    apply_url: str
    location: str = ""


@dataclass(frozen=True)
class AlertMessage:
    """One email, as the Gmail client hands it over."""
    sender: str  # bare address, lower-cased
    html: str


class _Mailbox(Protocol):  # what this module needs from the Gmail client
    def messages(self, query: str, limit: int) -> list[AlertMessage]: ...


def _without_query(url: str) -> str:
    """Drop the query string: portal links carry the reader's user id and auto-login tokens there."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_naukri(html: str) -> list[AlertJob]:
    """Naukri's "Jobs matching your job alert" email: one card per job.

    Each card links to naukri.com/jd/job-listings-...; the link that holds the title sits in a small
    table whose text reads title, company, (rating), location.
    """
    jobs: list[AlertJob] = []
    for link in BeautifulSoup(html, "html.parser").select('a[href*="naukri.com/jd/job-listings-"]'):
        if link.find("table"):  # the same link also wraps the whole card; that one isn't the title
            continue
        card = link.find_parent("table")
        role = link.get_text(" ", strip=True)
        texts = list(card.stripped_strings) if card else []
        if not role or len(texts) < 2:
            continue
        place = card.select_one("td.cart_subheading")
        jobs.append(AlertJob("Naukri", texts[1], role, _without_query(link["href"]),
                             place.get_text(" ", strip=True) if place else ""))
    return jobs


# sender address -> parser. Add a portal here once you have one of its real alert emails to test with.
PARSERS: dict[str, Callable[[str], list[AlertJob]]] = {
    "naukrialerts@naukri.com": parse_naukri,
}


def gmail_query(days: int) -> str:
    return f"from:({' OR '.join(PARSERS)}) newer_than:{max(days, 1)}d"


def is_internship(title: str) -> bool:
    return bool(_INTERN.search(title))


def jobs_in(messages: Iterable[AlertMessage], *, internships_only: bool = True) -> list[AlertJob]:
    """Every opening in these emails, newest email first, each apply link once."""
    seen: set[str] = set()
    jobs: list[AlertJob] = []
    for message in messages:
        parser = PARSERS.get(message.sender)
        if parser is None:
            continue
        try:
            found = parser(message.html)
        except Exception as exc:  # a portal changed its email layout: lose that email, not the run
            log.warning("Could not read a job-alert email from %s: %s", message.sender, exc)
            continue
        for job in found:
            if job.apply_url in seen or (internships_only and not is_internship(job.role)):
                continue
            seen.add(job.apply_url)
            jobs.append(job)
    return jobs


def read_alerts(mailbox: _Mailbox, days: int, *, internships_only: bool = True, limit: int = 30) -> list[AlertJob]:
    return jobs_in(mailbox.messages(gmail_query(days), limit), internships_only=internships_only)
