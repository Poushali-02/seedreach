"""Where each drafted company stands after the draft (#18), read from your own mailbox.

Gmail deletes a draft when it's sent and keeps a new copy, labelled SENT, under a new id. So:
- the draft is still in Drafts: not sent yet;
- the draft is gone and Sent has a message to that address since the draft: sent;
- after that, a delivery failure naming it: bounced; a person's reply from the company's domain: replied.

A reply has to look like one: a subject starting "Re:", from any address at the company's domain (founders often
answer from a colleague's address), and not marked as automatic or bulk mail. Auto-replies ("Support request
received", out of office) and product newsletters from the same company are not replies.

Reading Sent and the inbox needs Gmail's read-only scope. Without it, only the first check works (it needs
the draft scope alone), and a missing draft is recorded as "draft gone". Stages you set yourself with
`mark` (interview, offer, closed) are never changed here. Nothing is ever sent.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Optional, Protocol

from src.db import Database
from src.outreach.gmail_client import header_date

log = logging.getLogger(__name__)

REPLY_HEADERS = ["Date", "From", "Subject", "Auto-Submitted", "X-Autoreply", "X-Autorespond", "Precedence"]


class Mailbox(Protocol):
    def draft_exists(self, draft_id: str) -> bool: ...
    def message_dates(self, query: str, limit: int = 5) -> list[datetime]: ...
    def message_headers(self, query: str, names: list[str], limit: int = 5) -> list[dict[str, str]]: ...


def is_human_reply(headers: dict[str, str], domain: str) -> bool:
    """A person answering your email: "Re:" subject, from the company's domain, not automatic or bulk mail
    (RFC 3834's Auto-Submitted, the common X-Autoreply headers, Precedence). List-Unsubscribe is not a sign of
    bulk mail here: founders who send through a mailing service (Brevo, say) get it on personal replies too.
    Newsletters are left out by the "Re:" rule instead."""
    host = parseaddr(headers.get("from", ""))[1].lower().rsplit("@", 1)[-1]
    if not (host == domain or host.endswith("." + domain)):
        return False
    if not re.match(r"\s*re\s*:", headers.get("subject", ""), re.I):
        return False
    if headers.get("auto-submitted", "no").strip().lower() != "no":
        return False
    if headers.get("x-autoreply") or headers.get("x-autorespond"):
        return False
    return headers.get("precedence", "").strip().lower() not in ("bulk", "list", "junk", "auto_reply")


@dataclass(frozen=True)
class Change:
    domain: str
    company: str
    stage: str
    at: str
    who: str = ""  # for a reply: the sender's name as their email shows it, e.g. "Ada Lovelace"


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
            changes.append(Change(domain, company, *new))
    return changes


def _sender_name(headers: dict[str, str]) -> str:
    """The display name only ("Ada Lovelace"), never the address: it's shown in messages to your phone."""
    name = parseaddr(headers.get("from", ""))[0].strip().strip('"')
    return "" if "@" in name else name


def _next_stage(row, address: str, stage: Optional[str], mailbox: Mailbox,
                can_read: bool) -> Optional[tuple[str, ...]]:
    """(stage, when[, who]) the mailbox shows now, or None when nothing new can be told."""
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
        domain = address.rsplit("@", 1)[-1]
        found = mailbox.message_headers(f"from:{domain} after:{since}", REPLY_HEADERS, limit=10)
        replies = [(d, _sender_name(h)) for h in found
                   if is_human_reply(h, domain) and (d := header_date(h.get("date", "")))]
        if replies:
            when, who = min(replies)
            return ("replied", _iso(when), who)
    return None
