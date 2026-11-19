"""Where each drafted company stands after the draft (#18), read from your own mailbox.

Gmail deletes a draft when it's sent and keeps a new copy, labelled SENT, under a new id. So:
- the draft is still in Drafts: not sent yet;
- the draft is gone and Sent has a message to that address since the draft: sent;
- after that, a message from that address: replied; a delivery failure naming it: bounced.

Reading Sent and the inbox needs Gmail's read-only scope. Without it, only the first check works (it needs
the draft scope alone), and a missing draft is recorded as "draft gone". Stages you set yourself with
`mark` (interview, offer, closed) are never changed here. Nothing is ever sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from src.db import Database

log = logging.getLogger(__name__)


class Mailbox(Protocol):
    def draft_exists(self, draft_id: str) -> bool: ...
    def message_dates(self, query: str, limit: int = 5) -> list[datetime]: ...


@dataclass(frozen=True)
class Change:
    domain: str
    company: str
    stage: str
    at: str


def _day_before(iso: Optional[str]) -> str:
    """Gmail's after: takes a date (YYYY/MM/DD); a day early so nothing on the boundary day is missed."""
    when = datetime.fromisoformat(iso) if iso else datetime.now(timezone.utc)
    return (when - timedelta(days=1)).strftime("%Y/%m/%d")


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def update_stages(db: Database, mailbox: Mailbox, can_read: bool) -> list[Change]:
    """Check every drafted company still in play and record what changed. Never raises for one company:
    a Gmail error there is logged and that company is checked again next time."""
    changes: list[Change] = []
    for row in db.to_track():
        domain, address, stage = row["domain"], (row["founder_email"] or "").lower(), row["stage"]
        company = row["company_name"] or domain
        try:
            new = _next_stage(row, address, stage, mailbox, can_read)
        except Exception as exc:  # one company's mail can't stop the others being checked
            log.warning("Tracker: could not check %s (%s)", domain, exc)
            continue
        if new is not None and new[0] != stage:
            db.set_stage(domain, new[0], "mailbox", new[1])
            changes.append(Change(domain, company, new[0], new[1]))
    return changes


def _next_stage(row, address: str, stage: Optional[str], mailbox: Mailbox,
                can_read: bool) -> Optional[tuple[str, str]]:
    """(stage, when) the mailbox shows now, or None when nothing new can be told."""
    if stage is None:
        if row["draft_id"] and mailbox.draft_exists(row["draft_id"]):
            return None  # still in Drafts
        if not can_read:
            return ("draft gone", _iso(datetime.now(timezone.utc)))
        stage = "draft gone"
    if stage == "draft gone":
        if not can_read or not address:
            return None
        sent = mailbox.message_dates(f"in:sent to:{address} after:{_day_before(row['updated_at'])}")
        if not sent:
            return ("draft gone", _iso(datetime.now(timezone.utc))) if row["stage"] is None else None
        return ("sent", _iso(min(sent)))
    if stage == "sent" and can_read and address:
        since = _day_before(row["sent_at"])
        bounced = mailbox.message_dates(f'from:mailer-daemon "{address}" after:{since}')
        if bounced:
            return ("bounced", _iso(min(bounced)))
        replies = mailbox.message_dates(f"from:{address} after:{since}")
        if replies:
            return ("replied", _iso(min(replies)))
    return None
