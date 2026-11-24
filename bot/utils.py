"""Pure helpers, kept separate for unit tests."""

from __future__ import annotations

import html
import time
from typing import Any

# Telegram's Bot API deleteMessage hard limit is 48 hours. Keep a small safety
# margin so a delayed polling cycle does not cross the API cutoff.
TELEGRAM_DELETE_LIMIT_SECONDS = 48 * 60 * 60
DELETE_SAFETY_MARGIN_SECONDS = 120
MAX_SUPPORTED_DELETE_SECONDS = TELEGRAM_DELETE_LIMIT_SECONDS - DELETE_SAFETY_MARGIN_SECONDS

DELETE_OPTIONS: tuple[tuple[str, int], ...] = (
    ("Off", 0),
    ("30 minutes", 30 * 60),
    ("1 hour", 60 * 60),
    ("2 hours", 2 * 60 * 60),
    ("6 hours", 6 * 60 * 60),
    ("12 hours", 12 * 60 * 60),
    ("24 hours", 24 * 60 * 60),
    ("36 hours", 36 * 60 * 60),
    ("48 hours (safe cutoff)", MAX_SUPPORTED_DELETE_SECONDS),
)


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "Off"
    if seconds == MAX_SUPPORTED_DELETE_SECONDS:
        return "48 hours (safe cutoff)"
    if seconds % 86400 == 0:
        return f"{seconds // 86400} day(s)"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hour(s)"
    if seconds % 60 == 0:
        return f"{seconds // 60} minute(s)"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds_remainder = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour(s)")
    if minutes:
        parts.append(f"{minutes} minute(s)")
    if seconds_remainder:
        parts.append(f"{seconds_remainder} second(s)")
    return " ".join(parts)


def escape_html(text: str) -> str:
    """Escape dynamic values used in Telegram HTML messages."""
    return html.escape(text, quote=False)


def get_delete_due_at(now: int | None, seconds: int) -> int | None:
    """Return a safe deletion timestamp, or None when deletion is disabled."""
    if seconds <= 0:
        return None
    if seconds > MAX_SUPPORTED_DELETE_SECONDS:
        raise ValueError("Auto-delete cannot exceed Telegram's 48-hour deletion window.")
    base = int(time.time()) if now is None else int(now)
    return base + seconds


def build_copy_payload(target_chat_id: int, source_chat_id: int, source_message: dict[str, Any]) -> dict[str, Any]:
    """Build a copyMessage payload without modifying source content.

    Telegram's copyMessage removes the forward attribution. The source inline
    keyboard is passed as raw JSON, preserving its button definitions/styles.
    """
    payload: dict[str, Any] = {
        "chat_id": target_chat_id,
        "from_chat_id": source_chat_id,
        "message_id": int(source_message["message_id"]),
    }
    reply_markup = source_message.get("reply_markup")
    if isinstance(reply_markup, dict):
        payload["reply_markup"] = reply_markup
    return payload


def channel_label(channel: dict[str, Any]) -> str:
    title = str(channel.get("title") or "Untitled channel").strip()
    username = channel.get("username")
    return f"{title} (@{username})" if username else title
