"""SQLite state: guarantees we never draft to the same company or email twice."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from src.trace import Trace

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    domain        TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    source        TEXT NOT NULL,
    company_name  TEXT,
    founder_name  TEXT,
    founder_email TEXT,
    email_source  TEXT,
    status        TEXT NOT NULL CHECK (status IN ('queued', 'drafted', 'skipped')),
    draft_id      TEXT,
    reason        TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_drafted_email
    ON companies (founder_email) WHERE status = 'drafted' AND founder_email IS NOT NULL;
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    domain  TEXT,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    ts              TEXT NOT NULL,
    domain          TEXT NOT NULL,
    source          TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    detail          TEXT,
    pages           INTEGER,
    text_chars      INTEGER,
    truncated       INTEGER,
    model           TEXT,
    llm_calls       INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    founder_in_text INTEGER,
    email_source    TEXT,
    scrape_ms       INTEGER,
    extract_ms      INTEGER,
    email_ms        INTEGER,
    draft_ms        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_traces_run ON traces (run_id);
CREATE TABLE IF NOT EXISTS alert_jobs (
    apply_url  TEXT PRIMARY KEY,
    portal     TEXT NOT NULL,
    company    TEXT NOT NULL,
    role       TEXT NOT NULL,
    location   TEXT,
    first_seen TEXT NOT NULL
);
"""

# Changes to tables that already exist, applied once each, in order. `PRAGMA user_version` counts how many a
# database has had, so an old database (the runner's, say) catches up and a new one gets them all.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # 1. the tracker (#18): where each drafted company stands after the draft
    (
        "ALTER TABLE companies ADD COLUMN stage TEXT",          # see STAGES; NULL = drafted, nothing seen yet
        "ALTER TABLE companies ADD COLUMN stage_source TEXT",   # 'mailbox' (detected) or 'manual' (`mark`)
        "ALTER TABLE companies ADD COLUMN stage_at TEXT",       # when the stage last changed
        "ALTER TABLE companies ADD COLUMN sent_at TEXT",
        "ALTER TABLE companies ADD COLUMN replied_at TEXT",
    ),
    # 2. follow-ups (#17): at most one follow-up draft per company
    (
        "ALTER TABLE companies ADD COLUMN followup_draft_id TEXT",
        "ALTER TABLE companies ADD COLUMN followed_up_at TEXT",
    ),
)

# After the draft, in order. "draft gone" = the draft left Drafts but no sent copy was found (no read access,
# or it was deleted); "bounced" = Gmail reported the address undeliverable.
STAGES = ("draft gone", "sent", "bounced", "replied", "interview", "offer", "closed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        with self._conn() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for number, steps in enumerate(MIGRATIONS[version:], start=version + 1):
                for sql in steps:
                    conn.execute(sql)
                conn.execute(f"PRAGMA user_version = {number}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit / rollback
                yield conn
        finally:
            conn.close()

    # ---- dedupe checks -------------------------------------------------
    def is_known(self, domain: str) -> bool:
        """True once a company has been drafted or skipped (queued does not count)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM companies WHERE domain = ? AND status IN ('drafted', 'skipped')",
                (domain,),
            ).fetchone()
        return row is not None

    def status_of(self, domain: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT status FROM companies WHERE domain = ?", (domain,)).fetchone()
        return row["status"] if row else None

    def email_taken(self, email: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM companies WHERE founder_email = ? AND status = 'drafted'",
                (email.lower(),),
            ).fetchone()
        return row is not None

    # ---- writes --------------------------------------------------------
    def queue(self, domain: str, url: str, source: str) -> bool:
        """Add a lead to the work queue. Returns False if the domain is already tracked."""
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO companies (domain, url, source, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (domain, url, source, now, now),
            )
        return cur.rowcount == 1

    def _upsert(self, domain: str, url: str, source: str, status: str, **fields: Optional[str]) -> None:
        now = _now()
        cols = ["company_name", "founder_name", "founder_email", "email_source", "draft_id", "reason"]
        values = [fields.get(c) for c in cols]
        if fields.get("founder_email"):
            values[cols.index("founder_email")] = fields["founder_email"].lower()  # type: ignore[union-attr]
        with self._conn() as conn:
            conn.execute(
                f"""
                INSERT INTO companies (domain, url, source, status, {", ".join(cols)}, created_at, updated_at)
                VALUES (?, ?, ?, ?, {", ".join("?" * len(cols))}, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    status = excluded.status,
                    {", ".join(f"{c} = excluded.{c}" for c in cols)},
                    updated_at = excluded.updated_at
                """,
                (domain, url, source, status, *values, now, now),
            )

    def mark_drafted(
        self, domain: str, url: str, source: str, *, company_name: str, founder_name: Optional[str],
        founder_email: str, email_source: str, draft_id: str,
    ) -> None:
        self._upsert(
            domain, url, source, "drafted", company_name=company_name, founder_name=founder_name,
            founder_email=founder_email, email_source=email_source, draft_id=draft_id,
        )

    def mark_skipped(
        self, domain: str, url: str, source: str, reason: str, *, company_name: Optional[str] = None,
        founder_name: Optional[str] = None,
    ) -> None:
        self._upsert(
            domain, url, source, "skipped", company_name=company_name, founder_name=founder_name,
            reason=reason,
        )

    def alert_job_seen(self, apply_url: str) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM alert_jobs WHERE apply_url = ?", (apply_url,)).fetchone() is not None

    def add_alert_job(self, portal: str, company: str, role: str, apply_url: str, location: str = "") -> bool:
        """Remember an opening from a job-alert email. Returns False if an earlier run already listed it."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO alert_jobs (apply_url, portal, company, role, location, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)", (apply_url, portal, company, role, location, _now()),
            )
        return cur.rowcount == 1

    def log(self, level: str, message: str, domain: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (ts, level, domain, message) VALUES (?, ?, ?, ?)",
                (_now(), level.upper(), domain, message),
            )

    def record_trace(self, run_id: str, t: Trace) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO traces (run_id, ts, domain, source, outcome, detail, pages, text_chars, truncated, "
                "model, llm_calls, input_tokens, output_tokens, founder_in_text, email_source, "
                "scrape_ms, extract_ms, email_ms, draft_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, _now(), t.domain, t.source, t.outcome, t.detail, t.pages, t.text_chars, t.truncated,
                 t.model, t.llm_calls, t.input_tokens, t.output_tokens, t.founder_in_text, t.email_source,
                 t.ms.get("scrape"), t.ms.get("extract"), t.ms.get("email"), t.ms.get("draft")),
            )

    # ---- reads ---------------------------------------------------------
    def recent_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        """One summary row per run, newest first."""
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT run_id, COUNT(*) AS companies,
                       SUM(outcome = 'drafted') AS drafted, SUM(outcome = 'skipped') AS skipped,
                       SUM(outcome = 'error') AS errors,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(truncated), 0) AS truncated,
                       COALESCE(SUM(founder_in_text = 0), 0) AS founder_not_in_text,
                       AVG(COALESCE(scrape_ms, 0) + COALESCE(extract_ms, 0) + COALESCE(email_ms, 0)
                           + COALESCE(draft_ms, 0)) AS avg_ms
                FROM traces GROUP BY run_id ORDER BY MAX(id) DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def pending_queue(self, limit: int) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT domain, url, source FROM companies WHERE status = 'queued' "
                "ORDER BY created_at LIMIT ?", (limit,),
            ).fetchall()

    def stats(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM companies GROUP BY status").fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        return {
            "total": sum(counts.values()),
            "queued": counts.get("queued", 0),
            "drafted": counts.get("drafted", 0),
            "skipped": counts.get("skipped", 0),
        }

    def needs_contact(self, limit: int = 10) -> list[sqlite3.Row]:
        """Companies skipped only because no trustworthy address was found, newest first."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT domain, company_name, founder_name, reason FROM companies "
                "WHERE status = 'skipped' AND reason LIKE 'needs contact%' ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def recent_companies(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM companies WHERE status != 'queued' ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    # ---- tracker (#18) -------------------------------------------------
    def to_track(self) -> list[sqlite3.Row]:
        """Drafted companies whose next step the mailbox can still tell: not sent yet, or sent and waiting
        for a reply. Stages you set yourself with `mark` beyond those are left alone."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT domain, company_name, founder_email, draft_id, stage, sent_at, updated_at FROM companies "
                "WHERE status = 'drafted' AND (stage IS NULL OR stage IN ('draft gone', 'sent')) "
                "ORDER BY updated_at",
            ).fetchall()

    def set_stage(self, domain: str, stage: str, source: str, at: Optional[str] = None) -> bool:
        """Record a drafted company's stage; `at` is when it happened (default now). False if not drafted."""
        if stage not in STAGES:
            raise ValueError(f"Unknown stage {stage!r}; choose from: {', '.join(STAGES)}")
        at = at or _now()
        extra = {"sent": ", sent_at = COALESCE(sent_at, :at)", "replied": ", replied_at = COALESCE(replied_at, :at)"}
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE companies SET stage = :stage, stage_source = :source, stage_at = :at"
                + extra.get(stage, "") + " WHERE domain = :domain AND status = 'drafted'",
                {"stage": stage, "source": source, "at": at, "domain": domain},
            )
        return cur.rowcount == 1

    def stage_counts(self) -> dict[str, int]:
        """Drafted companies per stage; "drafted" counts those with nothing seen since the draft."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT COALESCE(stage, 'drafted') AS stage, COUNT(*) AS n FROM companies "
                "WHERE status = 'drafted' GROUP BY 1").fetchall()
        return {r["stage"]: r["n"] for r in rows}

    def tracked(self, stages: tuple[str, ...] = STAGES, limit: Optional[int] = None) -> list[sqlite3.Row]:
        """Drafted companies in the given stages, most recently changed first."""
        marks = ", ".join("?" * len(stages))
        sql = (f"SELECT * FROM companies WHERE status = 'drafted' AND stage IN ({marks}) "
               "ORDER BY stage_at DESC, rowid DESC")
        with self._conn() as conn:
            if limit is None:
                return conn.execute(sql, stages).fetchall()
            return conn.execute(sql + " LIMIT ?", (*stages, limit)).fetchall()

    def drafted(self) -> list[sqlite3.Row]:
        """Every drafted company with its stage, oldest draft first (for the CSV export)."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT domain, company_name, founder_name, founder_email, email_source, "
                "COALESCE(stage, 'drafted') AS stage, stage_source, updated_at AS drafted_at, sent_at, "
                "replied_at, stage_at, followed_up_at FROM companies WHERE status = 'drafted' "
                "ORDER BY updated_at").fetchall()

    # ---- follow-ups (#17) ----------------------------------------------
    def due_followups(self, sent_before: str, limit: int) -> list[sqlite3.Row]:
        """Companies you emailed before `sent_before` (ISO time) that haven't replied, bounced or been
        followed up yet, oldest first. Only the tracker's `sent` stage qualifies: a reply, a bounce or a stage
        you set yourself (interview, closed...) means no follow-up."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT domain, company_name, founder_name, founder_email, sent_at FROM companies "
                "WHERE status = 'drafted' AND stage = 'sent' AND followed_up_at IS NULL AND sent_at <= ? "
                "ORDER BY sent_at LIMIT ?", (sent_before, limit),
            ).fetchall()

    def mark_followed_up(self, domain: str, draft_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE companies SET followup_draft_id = ?, followed_up_at = ? WHERE domain = ?",
                         (draft_id, _now(), domain))

    def recent_events(self, limit: int = 15) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
