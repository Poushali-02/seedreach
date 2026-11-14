"""Save drafts to Gmail. This module can only create drafts; it never sends mail.

HARD CONSTRAINT: there is intentionally no code path calling drafts().send() or messages().send().

With JOB_ALERTS=1 it also reads job-alert emails (see src/sourcing/job_alerts.py). That needs Gmail's
read-only scope on top of the draft scope, so it is asked for only when the setting is on.
"""

from __future__ import annotations

import base64
import json
import logging
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.sourcing.job_alerts import AlertMessage

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"  # only with JOB_ALERTS=1


class GmailError(Exception):
    """Authentication, draft creation or reading mail failed."""


def build_raw_message(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body, subtype="plain", charset="utf-8")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def html_of(payload: dict) -> str:
    """The text/html body of a Gmail message payload (searching nested parts), or ""."""
    data = (payload.get("body") or {}).get("data")
    if payload.get("mimeType") == "text/html" and data:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    for part in payload.get("parts") or []:
        if html := html_of(part):
            return html
    return ""


class GmailDraftClient:
    def __init__(self, credentials_file: Path, token_file: Path, headless: bool = False,
                 read_mail: bool = False) -> None:
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.headless = headless
        self.scopes = SCOPES + [READ_SCOPE] if read_mail else list(SCOPES)
        self._service: Optional[Any] = None

    def _credentials(self) -> Credentials:
        creds: Optional[Credentials] = None
        missing_read = False
        if self.token_file.exists():
            # a token made before JOB_ALERTS was turned on can only write drafts: sign in again for it
            granted = json.loads(self.token_file.read_text(encoding="utf-8")).get("scopes") or []
            missing_read = not set(self.scopes) <= set(granted)
            if not missing_read:
                creds = Credentials.from_authorized_user_file(str(self.token_file), self.scopes)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self.token_file.write_text(creds.to_json(), encoding="utf-8")
                return creds
            except RefreshError:
                log.warning("Saved Gmail token expired or was revoked; re-authorising.")
        if self.headless and missing_read:
            raise GmailError(
                "JOB_ALERTS is on, but the saved Gmail token can only write drafts and HEADLESS is set. "
                "Run once on your own machine with JOB_ALERTS=1 to sign in again, then update the "
                "server's copy of token.json."
            )
        if self.headless:
            raise GmailError(
                "No valid Gmail token and HEADLESS is set, so no browser sign-in is possible. "
                "Run once on your own machine to create a fresh token.json, then update the server's copy."
            )
        if not self.credentials_file.exists():
            raise GmailError(
                f"{self.credentials_file.name} not found. Download an OAuth 'Desktop app' client from "
                "Google Cloud Console (see README)."
            )
        if missing_read:
            log.warning("JOB_ALERTS is on and the saved Gmail token can only write drafts; signing in again.")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), self.scopes)
        creds = flow.run_local_server(port=0)
        self.token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds

    @property
    def service(self) -> Any:
        if self._service is None:
            try:
                self._service = build("gmail", "v1", credentials=self._credentials(), cache_discovery=False)
            except GmailError:
                raise
            except Exception as exc:  # oauth flow / network
                raise GmailError(f"Gmail authentication failed: {exc}") from exc
        return self._service

    def ensure_authenticated(self) -> None:
        """Trigger the OAuth flow now so failures surface before any LLM spend."""
        _ = self.service

    def create_draft(self, to: str, subject: str, body: str) -> str:
        """Save a draft and return its Gmail draft id."""
        try:
            draft = self.service.users().drafts().create(
                userId="me", body={"message": {"raw": build_raw_message(to, subject, body)}}
            ).execute()
        except HttpError as exc:
            raise GmailError(f"Gmail API error: {exc}") from exc
        return draft["id"]

    def messages(self, query: str, limit: int = 30) -> list[AlertMessage]:
        """Emails matching a Gmail search, newest first: sender and HTML body only. Read-only.

        Spam is searched too (alert emails often land there); anything in Trash is left out.
        """
        try:
            listed = self.service.users().messages().list(
                userId="me", q=query, maxResults=limit, includeSpamTrash=True).execute()
            found: list[AlertMessage] = []
            for ref in listed.get("messages") or []:
                msg = self.service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
                if "TRASH" in (msg.get("labelIds") or []):
                    continue
                payload = msg.get("payload") or {}
                sender = next((h["value"] for h in payload.get("headers") or [] if h["name"].lower() == "from"), "")
                found.append(AlertMessage(parseaddr(sender)[1].lower(), html_of(payload)))
        except HttpError as exc:
            raise GmailError(f"Gmail API error: {exc}") from exc
        return found
