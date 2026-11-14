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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

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

    def recent_events(self, limit: int = 15) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
