"""Optional run summary to Telegram and/or Discord, so scheduled runs are easy to review later.

The message holds company names and counts only: never addresses, draft text or skip reasons
(some skip reasons contain an email address).
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from src.config import Settings
from src.pipeline import Result
from src.utils import display_name

log = logging.getLogger(__name__)

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _name(result: Result) -> str:
    return display_name(result.company) if result.company else result.domain


def build_summary(results: list[Result]) -> str:
    """e.g. 'seedreach: 3 new drafts: HillStar, Alter, Rhem Labs · 2 skipped'."""
    drafted = [_name(r) for r in results if r.outcome == "drafted"]
    skipped = sum(r.outcome == "skipped" for r in results)
    errors = sum(r.outcome == "error" for r in results)

    if drafted:
        head = f"{len(drafted)} new draft{'s' if len(drafted) != 1 else ''}: {', '.join(drafted)}"
    else:
        head = "no new drafts"
    parts = [head, f"{skipped} skipped"]
    if errors:
        parts.append(f"{errors} error{'s' if errors != 1 else ''}")
    return "seedreach: " + " · ".join(parts)


def _post(client: httpx.Client, channel: str, url: str, payload: dict) -> bool:
    try:
        client.post(url, json=payload).raise_for_status()
        return True
    except httpx.HTTPError as exc:
        # str(exc) includes the URL, which holds the bot token / webhook secret: log only the status or type
        why = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
        log.warning("%s summary not sent (%s)", channel, why)
        return False


def send_summary(text: str, settings: Settings, client: Optional[httpx.Client] = None) -> list[str]:
    """Post `text` to each configured channel; return the ones that worked. Failures only log a warning."""
    targets = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        targets.append(("Telegram", TELEGRAM_URL.format(token=settings.telegram_bot_token),
                        {"chat_id": settings.telegram_chat_id, "text": text}))
    if settings.discord_webhook_url:
        targets.append(("Discord", settings.discord_webhook_url, {"content": text}))
    if not targets:
        return []

    own_client = client is None
    client = client or httpx.Client(timeout=settings.http_timeout)
    try:
        return [channel for channel, url, payload in targets if _post(client, channel, url, payload)]
    finally:
        if own_client:
            client.close()
