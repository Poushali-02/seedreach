"""Find open internships on a company's public job board (Greenhouse, Lever or Ashby).

The scraper notices links to these boards on the company's own pages; this module reads each board's
free public JSON API and keeps internship roles. It never raises on network or data errors: a board
that can't be read is reported with a reason instead, so "checked, nothing open" stays distinct from
"couldn't check".

Public APIs (no key):
  Greenhouse  https://boards-api.greenhouse.io/v1/boards/<token>/jobs        {"jobs": [{"title", "absolute_url"}]}
  Lever       https://api.lever.co/v0/postings/<site>?mode=json              [{"text", "hostedUrl", "categories"}]
  Ashby       https://api.ashbyhq.com/posting-api/job-board/<name>           {"jobs": [{"title", "jobUrl", "isListed",
                                                                                "employmentType"}]}
An unknown board returns 404 on all three. Ashby can also return 200 with no jobs for a name that has
no board, so an empty Ashby result means "nothing found", not "no internships".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)

# board host -> ATS name. Paths look like /<token>, /<token>/jobs/<id>, or /embed/job_board?for=<token>.
_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}
_API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
}
# Board URLs anywhere in raw HTML: careers pages often carry them in page data, not in <a> tags.
_BOARD_URL = re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/[^\s\"'<>]+|"
                        r"https?://jobs\.lever\.co/[^\s\"'<>]+|https?://jobs\.ashbyhq\.com/[^\s\"'<>]+")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_NOT_TOKENS = {"embed", "jobs", "api", "v0", "v1"}

# Whole words only, so "Internal Tools" and "International" don't count.
_INTERN = re.compile(r"\b(intern(ship)?s?|new[ -]grad(uate)?s?|co-?op)\b", re.I)
# Only engineering/AI/data roles get named in a draft: a "Finance Intern" opening isn't one to mention.
_ENGINEERING = re.compile(
    r"\b(software|engineer(ing)?|developer|backend|back-end|full[ -]?stack|platform|infrastructure|"
    r"ai|ml|machine learning|data|scientist|deployment|forward[ -]deployed|devops|security|systems?)\b",
    re.I,
)
# Within those, roles closest to backend/AI work come first.
_PREFERRED = re.compile(r"\b(backend|back-end|ai|ml|machine learning|platform|infrastructure|data|python)\b", re.I)


@dataclass(frozen=True)
class Board:
    ats: str  # "greenhouse" | "lever" | "ashby"
    token: str

    @property
    def label(self) -> str:
        return f"{self.ats}:{self.token}"


@dataclass(frozen=True)
class Role:
    title: str
    url: str
    board: str  # Board.label


@dataclass
class BoardCheck:
    roles: list[Role] = field(default_factory=list)  # internship roles, any discipline
    problems: list[str] = field(default_factory=list)  # e.g. "lever:acme not_found"

    def best_role(self) -> Optional[Role]:
        """The engineering internship to name in a draft, preferring backend/AI/data; None if there is none."""
        engineering = [r for r in self.roles if _ENGINEERING.search(r.title)]
        if not engineering:
            return None
        return sorted(engineering, key=lambda r: not _PREFERRED.search(r.title))[0]


def board_urls_in(html: str) -> list[str]:
    """Every Greenhouse/Lever/Ashby URL in a page's raw HTML, including ones inside scripts or page data."""
    return _BOARD_URL.findall(html.replace("\\/", "/"))  # JSON often escapes slashes as \/


def find_boards(urls: Iterable[str]) -> list[Board]:
    """Job boards referenced by these URLs (links, iframes or scripts on the company's pages), deduplicated."""
    boards: list[Board] = []
    for raw in urls:
        try:
            parsed = urlparse(raw.strip())
        except ValueError:
            continue
        ats = _HOSTS.get((parsed.hostname or "").lower())
        if ats is None:
            continue
        token = (parse_qs(parsed.query).get("for") or [""])[0]  # Greenhouse embeds: /embed/job_board?for=<token>
        if not token:
            parts = [p for p in parsed.path.split("/") if p]
            token = parts[0] if parts else ""
        if token.lower() in _NOT_TOKENS or not _TOKEN.match(token):
            continue
        board = Board(ats, token)
        if board not in boards:
            boards.append(board)
    return boards


def _is_internship(title: str, *flags: object) -> bool:
    return bool(_INTERN.search(title)) or any(str(f).strip().lower() in {"intern", "internship"} for f in flags)


def _roles_from(board: Board, data: object) -> list[Role]:
    roles: list[Role] = []
    if board.ats == "lever":
        rows = data if isinstance(data, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("text") or "").strip()
            categories = row.get("categories")
            commitment = categories.get("commitment") if isinstance(categories, dict) else ""
            if title and _is_internship(title, commitment):
                roles.append(Role(title, str(row.get("hostedUrl") or ""), board.label))
        return roles
    rows = data.get("jobs") if isinstance(data, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if board.ats == "ashby":
            if row.get("isListed") is False:
                continue
            url, flags = row.get("jobUrl"), (row.get("employmentType"),)
        else:
            url, flags = row.get("absolute_url"), ()
        if title and _is_internship(title, *flags):
            roles.append(Role(title, str(url or ""), board.label))
    return roles


class JobBoardChecker:
    def __init__(self, timeout: float = 15.0, client: Optional[httpx.Client] = None) -> None:
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)

    def check(self, boards: Iterable[Board]) -> BoardCheck:
        result = BoardCheck()
        for board in boards:
            try:
                resp = self.client.get(_API[board.ats].format(token=board.token))
            except httpx.TimeoutException:
                result.problems.append(f"{board.label} timeout")
                continue
            except httpx.HTTPError as exc:
                result.problems.append(f"{board.label} unreachable")
                log.debug("job board %s unreachable: %s", board.label, exc)
                continue
            if resp.status_code == 404:
                result.problems.append(f"{board.label} not_found")
                continue
            if resp.status_code >= 400:
                result.problems.append(f"{board.label} http_{resp.status_code}")
                continue
            try:
                data = resp.json()
            except ValueError:
                result.problems.append(f"{board.label} bad_json")
                continue
            result.roles.extend(_roles_from(board, data))
        return result
