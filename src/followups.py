"""Follow-up drafts (#17): one short nudge for each company you emailed that hasn't answered.

A company qualifies when the tracker has it as `sent` (so it needs Gmail read access), you sent it at least
FOLLOW_UP_AFTER_DAYS ago, and it has no follow-up yet. A reply, a bounce, or a stage you set yourself
(interview, closed...) means no follow-up. The follow-up is a draft in the same Gmail conversation as your
first email, made at most once per company. Nothing is ever sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from src.db import Database
from src.outreach.generator import EmailDraft, generate_followup
from src.outreach.gmail_client import SentMessage
from src.profile import Profile
from src.tracker import _day_before

log = logging.getLogger(__name__)


class Drafts(Protocol):
    def first_sent(self, to: str, after: str) -> Optional[SentMessage]: ...
    def create_draft(self, to: str, subject: str, body: str, reply_to: Optional[SentMessage] = None) -> str: ...


@dataclass(frozen=True)
class FollowUp:
    domain: str
    company: str
    draft: EmailDraft
    draft_id: str  # "" in a dry run


def draft_followups(db: Database, gmail: Drafts, profile: Profile, after_days: int, limit: int,
                    dry_run: bool = False, now: Optional[datetime] = None) -> list[FollowUp]:
    """Draft follow-ups for up to `limit` companies emailed at least `after_days` ago, oldest first.
    `after_days` or `limit` of 0 turns them off. A dry run records nothing and creates no draft."""
    if after_days <= 0 or limit <= 0:
        return []
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=after_days)).isoformat(timespec="seconds")
    done: list[FollowUp] = []
    for row in db.due_followups(cutoff, limit):
        company = row["company_name"] or row["domain"]
        try:
            original = gmail.first_sent(row["founder_email"], _day_before(row["sent_at"]))
            draft = generate_followup(company, row["founder_name"], profile, datetime.fromisoformat(row["sent_at"]),
                                      original.subject if original else "")
            draft_id = "" if dry_run else gmail.create_draft(row["founder_email"], draft.subject, draft.body,
                                                             reply_to=original)
        except Exception as exc:  # one company can't stop the others; it's tried again next time
            log.warning("Follow-up for %s not drafted (%s)", row["domain"], exc)
            continue
        if not dry_run:
            db.mark_followed_up(row["domain"], draft_id)
        done.append(FollowUp(row["domain"], company, draft, draft_id))
    return done
