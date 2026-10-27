"""Save drafts to Gmail. This module can only create drafts; it never sends mail.

HARD CONSTRAINT: there is intentionally no code path calling drafts().send() or messages().send().
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


class GmailError(Exception):
    """Authentication or draft creation failed."""


def build_raw_message(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body, subtype="plain", charset="utf-8")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


class GmailDraftClient:
    def __init__(self, credentials_file: Path, token_file: Path) -> None:
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._service: Optional[Any] = None

    def _credentials(self) -> Credentials:
        creds: Optional[Credentials] = None
        if self.token_file.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self.token_file.write_text(creds.to_json(), encoding="utf-8")
                return creds
            except RefreshError:
                log.warning("Saved Gmail token expired or was revoked; re-authorising.")
        if not self.credentials_file.exists():
            raise GmailError(
                f"{self.credentials_file.name} not found. Download an OAuth 'Desktop app' client from "
                "Google Cloud Console (see README)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), SCOPES)
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
