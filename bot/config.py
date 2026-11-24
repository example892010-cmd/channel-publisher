"""Environment configuration for Channel Publisher Bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise ValueError(f"Invalid Telegram user ID in environment: {item!r}") from exc
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_ids: frozenset[int]
    admin_ids: frozenset[int]
    database_path: Path
    poll_timeout: int = 50
    send_delay_seconds: float = 0.10
    delete_check_interval: int = 10

    @property
    def trusted_user_ids(self) -> frozenset[int]:
        return self.owner_ids | self.admin_ids

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        owners = _parse_ids(os.getenv("OWNER_IDS", ""))
        admins = _parse_ids(os.getenv("ADMIN_IDS", ""))

        if not token:
            raise RuntimeError("BOT_TOKEN is missing. Add it to your environment variables.")
        if not owners:
            raise RuntimeError("OWNER_IDS is missing. Add at least one numeric Telegram user ID.")

        db_path = Path(os.getenv("DATABASE_PATH", "data/bot.db")).expanduser()
        poll_timeout = int(os.getenv("POLL_TIMEOUT", "50"))
        send_delay = float(os.getenv("SEND_DELAY_SECONDS", "0.10"))
        delete_interval = int(os.getenv("DELETE_CHECK_INTERVAL", "10"))

        if not 5 <= poll_timeout <= 60:
            raise ValueError("POLL_TIMEOUT must be between 5 and 60 seconds.")
        if not 0.0 <= send_delay <= 5.0:
            raise ValueError("SEND_DELAY_SECONDS must be between 0 and 5.")
        if not 5 <= delete_interval <= 300:
            raise ValueError("DELETE_CHECK_INTERVAL must be between 5 and 300 seconds.")

        return cls(
            bot_token=token,
            owner_ids=owners,
            admin_ids=admins,
            database_path=db_path,
            poll_timeout=poll_timeout,
            send_delay_seconds=send_delay,
            delete_check_interval=delete_interval,
        )
