"""Job-alert emails (#36): reading openings out of a portal's alert email and listing them in the digest.

The HTML fixture follows the layout of a real Naukri "Jobs matching your job alert" email (November
2026) with made-up companies and ids. No test touches Gmail or the network.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import src.outreach.gmail_client as gmail_client
from src.config import Settings
from src.db import Database
from src.digest import build_digest
from src.outreach.gmail_client import READ_SCOPE, SCOPES, GmailDraftClient, GmailError, html_of
from src.pipeline import Pipeline
from src.sourcing.job_alerts import AlertJob, AlertMessage, gmail_query, jobs_in, parse_naukri

NAUKRI = (Path(__file__).parent / "fixtures" / "job_alerts" / "naukri_alert.html").read_text("utf-8")
INTERN_URL = ("https://www.naukri.com/jd/job-listings-backend-engineering-intern-examplepay-bengaluru-"
              "0-to-1-years-091126000001")


def test_naukri_alert_gives_company_role_place_and_a_clean_link():
    jobs = parse_naukri(NAUKRI)
    assert jobs == [
        AlertJob("Naukri", "ExamplePay Technologies", "Backend Engineering Intern", INTERN_URL, "Bengaluru"),
        AlertJob("Naukri", "Samplestack Labs", "AI Engineer",
                 "https://www.naukri.com/jd/job-listings-ai-engineer-samplestack-labs-remote-2-to-5-years-051126000002",
                 "Remote"),
    ]
    # the email's links carry the reader's user id and auto-login parameters: none may survive
    assert all("?" not in j.apply_url and "uid" not in j.apply_url for j in jobs)


def test_only_internships_by_default_and_each_link_once():
    naukri = AlertMessage("naukrialerts@naukri.com", NAUKRI)
    assert [j.role for j in jobs_in([naukri, naukri])] == ["Backend Engineering Intern"]
    assert [j.role for j in jobs_in([naukri], internships_only=False)] == ["Backend Engineering Intern", "AI Engineer"]


def test_unknown_senders_and_broken_emails_are_ignored():
    assert jobs_in([AlertMessage("promo@naukri.com", NAUKRI)]) == []  # no parser for that sender
    assert jobs_in([AlertMessage("naukrialerts@naukri.com", "<p>new layout</p>")]) == []
    assert "naukrialerts@naukri.com" in gmail_query(3) and gmail_query(0).endswith("newer_than:1d")


def test_html_body_is_found_in_nested_parts():
    data = base64.urlsafe_b64encode("<p>héllo</p>".encode()).decode().rstrip("=")
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain", "body": {"data": "eA"}},
        {"mimeType": "multipart/related", "parts": [{"mimeType": "text/html", "body": {"data": data}}]},
    ]}
    assert html_of(payload) == "<p>héllo</p>"
    assert html_of({"mimeType": "text/plain", "body": {"data": "eA"}}) == ""


def test_read_scope_is_asked_for_only_with_job_alerts(tmp_path: Path):
    plain = GmailDraftClient(tmp_path / "c.json", tmp_path / "t.json")
    reading = GmailDraftClient(tmp_path / "c.json", tmp_path / "t.json", read_mail=True)
    assert plain.scopes == SCOPES and READ_SCOPE not in SCOPES
    assert reading.scopes == SCOPES + [READ_SCOPE]


def test_a_drafts_only_token_is_not_used_for_reading(tmp_path: Path):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "x", "refresh_token": "y", "client_id": "i", "client_secret": "s",
                                 "scopes": SCOPES}), encoding="utf-8")
    client = GmailDraftClient(tmp_path / "credentials.json", token, headless=True, read_mail=True)
    with pytest.raises(GmailError, match="JOB_ALERTS"):
        client.ensure_authenticated()


class _Mailbox:
    def __init__(self, messages=None, error: Exception | None = None) -> None:
        self._messages, self._error, self.queries = messages or [], error, []

    def messages(self, query: str, limit: int = 30):
        self.queries.append(query)
        if self._error:
            raise self._error
        return self._messages


def _pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, dry_run: bool, **settings) -> Pipeline:
    monkeypatch.setattr("src.pipeline.load_profile", lambda: {})
    monkeypatch.setattr(gmail_client.GmailDraftClient, "ensure_authenticated", lambda self: None)
    return Pipeline(Settings(gemini_api_key="k", **settings), Database(tmp_path / "t.db"), dry_run=dry_run)


def test_run_lists_each_alert_opening_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pipe = _pipeline(tmp_path, monkeypatch, dry_run=False, job_alerts=True)
    pipe.gmail = _Mailbox([AlertMessage("naukrialerts@naukri.com", NAUKRI)])
    first = pipe.job_alerts()
    assert [(j.company, j.role) for j in first] == [("ExamplePay Technologies", "Backend Engineering Intern")]
    assert pipe.job_alerts() == []  # already listed by the run above
    assert "newer_than:3d" in pipe.gmail.queries[0]

    digest = build_digest([], alerts=first)
    assert "### From your job alerts" in digest
    assert f"- **ExamplePay Technologies**: [Backend Engineering Intern]({INTERN_URL}), Bengaluru (Naukri)" in digest
    assert build_digest([]) == ""


def test_dry_run_shows_alerts_without_recording_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pipe = _pipeline(tmp_path, monkeypatch, dry_run=True, job_alerts=True)
    pipe.gmail = _Mailbox([AlertMessage("naukrialerts@naukri.com", NAUKRI)])
    assert len(pipe.job_alerts()) == 1
    assert len(pipe.job_alerts()) == 1  # nothing was saved, so a real run would still list it


def test_alerts_are_off_by_default_and_never_fail_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    off = _pipeline(tmp_path, monkeypatch, dry_run=False)
    off.gmail = _Mailbox(error=AssertionError("mail must not be read when JOB_ALERTS is off"))
    assert off.job_alerts() == []

    on = _pipeline(tmp_path, monkeypatch, dry_run=False, job_alerts=True)
    on.gmail = _Mailbox(error=GmailError("insufficient permission"))
    assert on.job_alerts() == []
