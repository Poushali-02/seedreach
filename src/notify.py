"""Optional messages to Telegram and/or Discord: a one-line run summary, and a brief when a founder replies.

The run summary holds company names and counts only: never addresses, draft text or skip reasons (some skip
reasons contain an email address). A reply brief (src/briefs.py) holds the sender's name and what the
company's public site says: no addresses and no email text.
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
TELEGRAM_LIMIT, DISCORD_LIMIT = 4096, 2000  # per message, longer ones are refused; counted in UTF-16 units


def _units(text: str) -> int:
    """Length as the apps count it: UTF-16 code units, so an emoji or other character beyond U+FFFF is 2."""
    return len(text.encode("utf-16-le")) // 2


def _fit(text: str, limit: int) -> str:
    """`text`, cut at a line break to fit `limit` UTF-16 units when it's longer, with a note that it was cut.
    Cuts fall between whole characters, never inside one."""
    if _units(text) <= limit:
        return text
    note = "\n(cut to fit this app)"
    budget, used, end = limit - _units(note), 0, 0
    for end, ch in enumerate(text):
        used += _units(ch)
        if used > budget:
            break
    head = text[:end]
    return (head.rsplit("\n", 1)[0] if "\n" in head else head) + note


def _name(result: Result) -> str:
    return display_name(result.company) if result.company else result.domain


def build_summary(results: list[Result], alert_count: int = 0) -> str:
    """e.g. 'seedreach: 3 new drafts: HillStar, Alter · 2 skipped · 4 new from job alerts'."""
    drafted = [_name(r) for r in results if r.outcome == "drafted"]
    skipped = sum(r.outcome == "skipped" for r in results)
    errors = sum(r.outcome == "error" for r in results)

    if drafted:
        head = f"{len(drafted)} new draft{'s' if len(drafted) != 1 else ''}: {', '.join(drafted)}"
    else:
        head = "no new drafts"
    parts = [head, f"{skipped} skipped"]
    if alert_count:
        parts.append(f"{alert_count} new from job alerts")
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


def configured(settings: Settings) -> bool:
    """Whether Telegram or Discord is set up."""
    return bool((settings.telegram_bot_token and settings.telegram_chat_id) or settings.discord_webhook_url)


def send_summary(text: str, settings: Settings, client: Optional[httpx.Client] = None) -> list[str]:
    """Post `text` to each configured channel; return the ones that worked. Failures only log a warning."""
    targets = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        targets.append(("Telegram", TELEGRAM_URL.format(token=settings.telegram_bot_token),
                        {"chat_id": settings.telegram_chat_id, "text": _fit(text, TELEGRAM_LIMIT)}))
    if settings.discord_webhook_url:
        targets.append(("Discord", settings.discord_webhook_url, {"content": _fit(text, DISCORD_LIMIT)}))
    if not targets:
        return []

    own_client = client is None
    client = client or httpx.Client(timeout=settings.http_timeout)
    try:
        return [channel for channel, url, payload in targets if _post(client, channel, url, payload)]
    finally:
        if own_client:
            client.close()
