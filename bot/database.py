"""Small SQLite persistence layer. No channel-count cap is imposed."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS channels (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                username TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                added_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS template_channels (
                template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL REFERENCES channels(chat_id) ON DELETE CASCADE,
                PRIMARY KEY (template_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_states (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                data_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS scheduled_deletions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                due_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                UNIQUE(chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                target_chat_id INTEGER NOT NULL,
                target_message_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(source_chat_id, source_message_id, target_chat_id)
            );

            CREATE INDEX IF NOT EXISTS idx_deletions_due
                ON scheduled_deletions(due_at);
            CREATE INDEX IF NOT EXISTS idx_template_channels_chat
                ON template_channels(chat_id);
            """
        )
        self.connection.commit()
        self.set_setting("auto_delete_seconds", self.get_setting("auto_delete_seconds", "0"))
        self.set_setting("default_target", self.get_setting("default_target", "all"))

    def close(self) -> None:
        self.connection.close()

    def _commit(self) -> None:
        self.connection.commit()

    # Settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str | int) -> None:
        self.connection.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self._commit()

    def auto_delete_seconds(self) -> int:
        try:
            return max(0, int(self.get_setting("auto_delete_seconds", "0") or 0))
        except (TypeError, ValueError):
            return 0

    # Channels
    def register_channel(self, chat_id: int, title: str, username: str | None) -> None:
        now = int(time.time())
        self.connection.execute(
            "INSERT INTO channels(chat_id,title,username,active,added_at,updated_at) "
            "VALUES(?,?,?,1,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, "
            "username=excluded.username, active=1, updated_at=excluded.updated_at",
            (chat_id, title or "Untitled channel", username, now, now),
        )
        self._commit()

    def deactivate_channel(self, chat_id: int) -> None:
        self.connection.execute(
            "UPDATE channels SET active=0, updated_at=? WHERE chat_id=?",
            (int(time.time()), chat_id),
        )
        self._commit()

    def get_channels(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM channels"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY title COLLATE NOCASE"
        return [dict(row) for row in self.connection.execute(sql).fetchall()]

    def get_channel(self, chat_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM channels WHERE chat_id=?", (chat_id,)).fetchone()
        return dict(row) if row else None

    def active_channel_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS n FROM channels WHERE active=1").fetchone()
        return int(row["n"])

    # Templates
    def create_template(self, name: str, created_by: int) -> int:
        cur = self.connection.execute(
            "INSERT INTO templates(name,created_by,created_at) VALUES(?,?,?)",
            (name, created_by, int(time.time())),
        )
        self._commit()
        return int(cur.lastrowid)

    def get_templates(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT t.*, COUNT(CASE WHEN c.active=1 THEN tc.chat_id END) AS channel_count "
            "FROM templates t LEFT JOIN template_channels tc ON tc.template_id=t.id "
            "LEFT JOIN channels c ON c.chat_id=tc.chat_id "
            "GROUP BY t.id ORDER BY t.id DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_template(self, template_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        return dict(row) if row else None

    def add_channel_to_template(self, template_id: int, chat_id: int) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO template_channels(template_id,chat_id) VALUES(?,?)",
            (template_id, chat_id),
        )
        self._commit()

    def remove_channel_from_template(self, template_id: int, chat_id: int) -> None:
        self.connection.execute(
            "DELETE FROM template_channels WHERE template_id=? AND chat_id=?",
            (template_id, chat_id),
        )
        self._commit()

    def template_contains(self, template_id: int, chat_id: int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM template_channels WHERE template_id=? AND chat_id=?",
            (template_id, chat_id),
        ).fetchone()
        return row is not None

    def get_template_channels(self, template_id: int, active_only: bool = True) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.* FROM channels c JOIN template_channels tc ON tc.chat_id=c.chat_id "
            "WHERE tc.template_id=?"
        )
        if active_only:
            sql += " AND c.active=1"
        sql += " ORDER BY c.title COLLATE NOCASE"
        return [dict(row) for row in self.connection.execute(sql, (template_id,)).fetchall()]

    def delete_template(self, template_id: int) -> None:
        self.connection.execute("DELETE FROM templates WHERE id=?", (template_id,))
        if self.get_setting("default_target") == f"template:{template_id}":
            self.set_setting("default_target", "all")
        self._commit()

    def resolve_target_channels(self) -> tuple[str, list[dict[str, Any]]]:
        target = self.get_setting("default_target", "all") or "all"
        if target == "all":
            return "All connected channels", self.get_channels(active_only=True)
        if target.startswith("template:"):
            try:
                template_id = int(target.split(":", 1)[1])
            except ValueError:
                self.set_setting("default_target", "all")
                return "All connected channels", self.get_channels(active_only=True)
            template = self.get_template(template_id)
            if not template:
                self.set_setting("default_target", "all")
                return "All connected channels", self.get_channels(active_only=True)
            return str(template["name"]), self.get_template_channels(template_id, active_only=True)
        self.set_setting("default_target", "all")
        return "All connected channels", self.get_channels(active_only=True)

    # Per-admin wizard state
    def set_admin_state(self, user_id: int, state: str, data: dict[str, Any] | None = None) -> None:
        self.connection.execute(
            "INSERT INTO admin_states(user_id,state,data_json) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data_json=excluded.data_json",
            (user_id, state, json.dumps(data or {})),
        )
        self._commit()

    def get_admin_state(self, user_id: int) -> tuple[str, dict[str, Any]] | None:
        row = self.connection.execute(
            "SELECT state,data_json FROM admin_states WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["data_json"])
        except json.JSONDecodeError:
            data = {}
        return str(row["state"]), data if isinstance(data, dict) else {}

    def clear_admin_state(self, user_id: int) -> None:
        self.connection.execute("DELETE FROM admin_states WHERE user_id=?", (user_id,))
        self._commit()


    # Idempotency for retried Telegram updates
    def get_delivered_target_ids(self, source_chat_id: int, source_message_id: int) -> set[int]:
        rows = self.connection.execute(
            "SELECT target_chat_id FROM broadcast_deliveries WHERE source_chat_id=? AND source_message_id=?",
            (source_chat_id, source_message_id),
        ).fetchall()
        return {int(row["target_chat_id"]) for row in rows}

    def record_broadcast_delivery(
        self,
        source_chat_id: int,
        source_message_id: int,
        target_chat_id: int,
        target_message_id: int,
        due_at: int | None,
    ) -> None:
        now = int(time.time())
        self.connection.execute(
            "INSERT OR IGNORE INTO broadcast_deliveries "
            "(source_chat_id,source_message_id,target_chat_id,target_message_id,created_at) "
            "VALUES(?,?,?,?,?)",
            (source_chat_id, source_message_id, target_chat_id, target_message_id, now),
        )
        if due_at is not None:
            self.connection.execute(
                "INSERT OR REPLACE INTO scheduled_deletions "
                "(chat_id,message_id,due_at,attempts,last_error) VALUES(?,?,?,0,NULL)",
                (target_chat_id, target_message_id, due_at),
            )
        self._commit()

    # Persistent auto-delete jobs
    def schedule_delete(self, chat_id: int, message_id: int, due_at: int) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO scheduled_deletions(chat_id,message_id,due_at,attempts,last_error) "
            "VALUES(?,?,?,0,NULL)",
            (chat_id, message_id, due_at),
        )
        self._commit()

    def get_due_deletions(self, now: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM scheduled_deletions WHERE due_at<=? ORDER BY due_at LIMIT ?",
            (now, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def complete_delete(self, job_id: int) -> None:
        self.connection.execute("DELETE FROM scheduled_deletions WHERE id=?", (job_id,))
        self._commit()

    def retry_delete(self, job_id: int, next_due_at: int, error: str) -> None:
        self.connection.execute(
            "UPDATE scheduled_deletions SET due_at=?, attempts=attempts+1, last_error=? WHERE id=?",
            (next_due_at, error[:500], job_id),
        )
        self._commit()

    def pending_delete_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS n FROM scheduled_deletions").fetchone()
        return int(row["n"])
