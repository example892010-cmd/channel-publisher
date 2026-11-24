"""Telegram channel publisher application and admin panel."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import Settings
from .database import Database
from .telegram_api import TelegramAPIError, TelegramClient
from .utils import (
    DELETE_OPTIONS,
    build_copy_payload,
    channel_label,
    escape_html,
    format_duration,
    get_delete_due_at,
)

log = logging.getLogger("channel_publisher_bot")


class ChannelPublisherBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.database_path)
        self.api = TelegramClient(settings.bot_token)
        self.bot_id: int | None = None
        self.bot_username: str | None = None
        self.stopping = False

    async def close(self) -> None:
        self.stopping = True
        self.db.close()
        await self.api.close()

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.settings.trusted_user_ids

    async def start(self) -> None:
        await self.api.call("deleteWebhook", {"drop_pending_updates": False})
        me = await self.api.call("getMe", {})
        self.bot_id = int(me["id"])
        self.bot_username = me.get("username")
        log.info("Bot started as @%s (id=%s)", self.bot_username, self.bot_id)
        await self._notify_owners(
            "✅ Channel Publisher Bot is online.\n"
            f"Connected channels: {self.db.active_channel_count()}\n"
            f"Auto-delete: {format_duration(self.db.auto_delete_seconds())}"
        )

        delete_task = asyncio.create_task(self._deletion_worker(), name="auto-delete-worker")
        try:
            await self._poll_updates()
        finally:
            delete_task.cancel()
            try:
                await delete_task
            except asyncio.CancelledError:
                pass

    async def _poll_updates(self) -> None:
        offset: int | None = None
        allowed_updates = ["message", "my_chat_member", "callback_query"]
        while not self.stopping:
            payload: dict[str, Any] = {
                "timeout": self.settings.poll_timeout,
                "allowed_updates": allowed_updates,
            }
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = await self.api.call("getUpdates", payload)
                for update in updates or []:
                    offset = int(update["update_id"]) + 1
                    try:
                        await self.handle_update(update)
                    except Exception:
                        log.exception("Failed to handle update_id=%s", update.get("update_id"))
            except TelegramAPIError as exc:
                log.error("Polling error: %s", exc)
                if exc.retry_after:
                    await asyncio.sleep(exc.retry_after + 1)
                else:
                    await asyncio.sleep(3)
            except Exception:
                log.exception("Polling network/runtime error")
                await asyncio.sleep(3)

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "my_chat_member" in update:
            await self.handle_my_chat_member(update["my_chat_member"])
        elif "callback_query" in update:
            await self.handle_callback(update["callback_query"])
        elif "message" in update:
            await self.handle_message(update["message"])

    # ------------------------------------------------------------------
    # Incoming messages / copy-based publishing
    # ------------------------------------------------------------------
    async def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        user_id = sender.get("id")
        if chat.get("type") != "private":
            return
        if not self.is_admin(user_id):
            await self.api.call("sendMessage", {
                "chat_id": chat["id"],
                "text": "This bot is private. Ask the bot owner to add your Telegram ID as an admin.",
            })
            return

        text_value = message.get("text")
        if isinstance(text_value, str) and text_value.startswith("/"):
            command = text_value.split()[0].split("@")[0].lower()
            if command in ("/start", "/help"):
                await self.show_panel(int(chat["id"]))
                return
            if command in ("/admin", "/panel"):
                await self.show_panel(int(chat["id"]))
                return
            if command == "/cancel":
                self.db.clear_admin_state(int(user_id))
                await self.api.call("sendMessage", {
                    "chat_id": chat["id"],
                    "text": "Cancelled. Use /admin to open the control panel.",
                })
                return

        state = self.db.get_admin_state(int(user_id))
        if state and state[0] == "template_name":
            if not isinstance(text_value, str):
                await self.api.call("sendMessage", {
                    "chat_id": chat["id"],
                    "text": "Please send the template name as text, or use /cancel.",
                })
                return
            name = text_value.strip()
            if not name or len(name) > 40:
                await self.api.call("sendMessage", {
                    "chat_id": chat["id"],
                    "text": "Template name must contain 1–40 characters. Try again, or use /cancel.",
                })
                return
            template_id = self.db.create_template(name, int(user_id))
            self.db.set_admin_state(int(user_id), "template_capture", {"template_id": template_id})
            await self.show_template_capture(int(chat["id"]), template_id)
            return

        # Never attempt to bypass Telegram protected-content restrictions.
        if message.get("has_protected_content") is True:
            await self.api.call("sendMessage", {
                "chat_id": chat["id"],
                "text": "This message is marked as protected content by Telegram, so it cannot be copied to channels.",
            })
            return

        # A new text/media/forward sent to the bot is treated as a post.
        if not self._has_copyable_content(message):
            await self.api.call("sendMessage", {
                "chat_id": chat["id"],
                "text": "Send or forward a text, photo, video, document, audio, sticker, poll, or other copyable Telegram message.",
            })
            return
        await self.publish_message(message, int(chat["id"]))

    @staticmethod
    def _has_copyable_content(message: dict[str, Any]) -> bool:
        # Telegram also supports a number of less common Message types. This
        # allow-list catches service updates and empty private-chat updates.
        supported = {
            "text", "animation", "audio", "document", "photo", "sticker",
            "video", "video_note", "voice", "contact", "dice", "game",
            "location", "venue", "poll", "story", "live_photo", "checklist",
            "paid_media", "gift", "unique_gift", "invoice", "web_app_data",
        }
        return any(key in message for key in supported)

    async def publish_message(self, source_message: dict[str, Any], source_chat_id: int) -> None:
        target_name, targets = self.db.resolve_target_channels()
        if not targets:
            await self.api.call("sendMessage", {
                "chat_id": source_chat_id,
                "text": (
                    f"No active channels found for target: {target_name}.\n"
                    "Add this bot as an administrator in your channels, then send the post again."
                ),
            })
            return

        status = await self.api.call("sendMessage", {
            "chat_id": source_chat_id,
            "text": f"⏳ Publishing to {len(targets)} channel(s) using <b>{escape_html(target_name)}</b>…",
            "parse_mode": "HTML",
        })
        source_message_id = int(source_message["message_id"])
        delivered_ids = self.db.get_delivered_target_ids(source_chat_id, source_message_id)
        success = sum(1 for channel in targets if int(channel["chat_id"]) in delivered_ids)
        failures: list[str] = []
        delete_seconds = self.db.auto_delete_seconds()

        for index, channel in enumerate(targets):
            chat_id = int(channel["chat_id"])
            if chat_id in delivered_ids:
                continue
            try:
                payload = build_copy_payload(chat_id, source_chat_id, source_message)
                result = await self.api.call_with_retry("copyMessage", payload)
                if isinstance(result, dict) and result.get("message_id"):
                    due_at = get_delete_due_at(int(time.time()), delete_seconds)
                    self.db.record_broadcast_delivery(
                        source_chat_id,
                        source_message_id,
                        chat_id,
                        int(result["message_id"]),
                        due_at,
                    )
                success += 1
            except TelegramAPIError as exc:
                label = channel_label(channel)
                failures.append(f"• {label}: {exc.description[:120]}")
                log.warning("Could not copy message to %s: %s", label, exc)
            except Exception as exc:
                label = channel_label(channel)
                failures.append(f"• {label}: {str(exc)[:120]}")
                log.exception("Unexpected copy failure for channel %s", chat_id)

            if self.settings.send_delay_seconds and index < len(targets) - 1:
                await asyncio.sleep(self.settings.send_delay_seconds)

        summary = (
            "✅ Publishing finished\n"
            f"Target: {target_name}\n"
            f"Successful: {success}/{len(targets)}\n"
            f"Failed: {len(failures)}\n"
            f"Auto-delete: {format_duration(delete_seconds)}"
        )
        if failures:
            summary += "\n\nFailed channels:\n" + "\n".join(failures[:15])
            if len(failures) > 15:
                summary += f"\n…and {len(failures) - 15} more. Check the bot logs."
        await self._edit_or_send(int(source_chat_id), int(status["message_id"]), summary)

    # ------------------------------------------------------------------
    # Bot added / removed from channels
    # ------------------------------------------------------------------
    async def handle_my_chat_member(self, update: dict[str, Any]) -> None:
        chat = update.get("chat") or {}
        if chat.get("type") != "channel":
            return
        new_member = update.get("new_chat_member") or {}
        old_member = update.get("old_chat_member") or {}
        new_status = new_member.get("status")
        old_status = old_member.get("status")
        chat_id = int(chat["id"])
        actor_id = (update.get("from") or {}).get("id")

        if new_status in ("administrator", "creator"):
            title = chat.get("title") or chat.get("username") or f"Channel {chat_id}"
            self.db.register_channel(chat_id, title, chat.get("username"))

            # If an administrator is in the Create Template wizard, newly
            # connected channels are automatically placed in that template.
            if actor_id is not None:
                state = self.db.get_admin_state(int(actor_id))
                if state and state[0] == "template_capture":
                    template_id = int(state[1].get("template_id", 0))
                    if template_id and self.db.get_template(template_id):
                        self.db.add_channel_to_template(template_id, chat_id)

            # This confirmation is a normal post to the channel, as requested.
            try:
                await self.api.call_with_retry("sendMessage", {
                    "chat_id": chat_id,
                    "text": "✅ Bot added successfully! This channel is now connected.",
                })
            except Exception as exc:
                log.warning("Channel %s registered, but confirmation could not be posted: %s", chat_id, exc)
                await self._notify_owners(
                    f"⚠️ {title} was registered, but I could not post the welcome confirmation. "
                    "Check that the bot has permission to post messages."
                )

            await self._notify_owners(f"✅ Channel connected: {title} ({chat_id})")
            if actor_id is not None and self.is_admin(int(actor_id)):
                try:
                    await self.api.call("sendMessage", {
                        "chat_id": actor_id,
                        "text": f"✅ Connected: {title}" + (
                            "\nAdded to your template in progress." if self._is_capturing(actor_id) else ""
                        ),
                    })
                except Exception:
                    pass

        elif new_status in ("left", "kicked", "member") and old_status in ("administrator", "creator"):
            self.db.deactivate_channel(chat_id)
            title = chat.get("title") or f"Channel {chat_id}"
            await self._notify_owners(f"⚠️ Bot is no longer an administrator in {title}; channel disabled.")

    def _is_capturing(self, user_id: int) -> bool:
        state = self.db.get_admin_state(user_id)
        return bool(state and state[0] == "template_capture")

    # ------------------------------------------------------------------
    # Admin panel callbacks
    # ------------------------------------------------------------------
    async def handle_callback(self, query: dict[str, Any]) -> None:
        query_id = query.get("id")
        sender_id = (query.get("from") or {}).get("id")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        message_id = message.get("message_id")
        if query_id:
            await self.api.call("answerCallbackQuery", {"callback_query_id": query_id})
        if not self.is_admin(sender_id) or chat.get("type") != "private":
            return
        data = str(query.get("data") or "")
        chat_id = int(chat["id"])
        user_id = int(sender_id)

        try:
            if data == "panel" or data == "back":
                await self.show_panel(chat_id, message_id)
            elif data == "channels":
                await self.show_channels(chat_id, message_id, 0)
            elif data.startswith("channels:"):
                await self.show_channels(chat_id, message_id, int(data.split(":", 1)[1]))
            elif data == "templates":
                await self.show_templates(chat_id, message_id)
            elif data == "template_create":
                self.db.set_admin_state(user_id, "template_name", {})
                await self._edit_or_send(chat_id, message_id, "🧩 Send a name for this channel template.\n\nUse /cancel to stop.")
            elif data.startswith("template_done:"):
                template_id = int(data.split(":", 1)[1])
                state = self.db.get_admin_state(user_id)
                if state and state[0] == "template_capture" and int(state[1].get("template_id", 0)) == template_id:
                    self.db.clear_admin_state(user_id)
                await self.show_templates(chat_id, message_id, note="✅ Template saved. Newly added channels will no longer be auto-assigned to it.")
            elif data.startswith("template_cancel:"):
                template_id = int(data.split(":", 1)[1])
                state = self.db.get_admin_state(user_id)
                if state and state[0] == "template_capture" and int(state[1].get("template_id", 0)) == template_id:
                    self.db.clear_admin_state(user_id)
                await self.show_templates(chat_id, message_id, note="Template capture stopped. The template was kept.")
            elif data.startswith("template_existing:"):
                _, template_id, page = data.split(":", 2)
                await self.show_template_channel_picker(chat_id, message_id, int(template_id), int(page))
            elif data.startswith("template_toggle:"):
                _, template_id, selected_chat_id, page = data.split(":", 3)
                template_id = int(template_id)
                selected_chat_id = int(selected_chat_id)
                if self.db.template_contains(template_id, selected_chat_id):
                    self.db.remove_channel_from_template(template_id, selected_chat_id)
                else:
                    self.db.add_channel_to_template(template_id, selected_chat_id)
                await self.show_template_channel_picker(chat_id, message_id, template_id, int(page))
            elif data.startswith("template_delete:"):
                template_id = int(data.split(":", 1)[1])
                self.db.delete_template(template_id)
                await self.show_templates(chat_id, message_id, note="Template deleted.")
            elif data == "target":
                await self.show_target_picker(chat_id, message_id)
            elif data == "target_all":
                self.db.set_setting("default_target", "all")
                await self.show_target_picker(chat_id, message_id, note="✅ Default target set to all connected channels.")
            elif data.startswith("target_template:"):
                template_id = int(data.split(":", 1)[1])
                if self.db.get_template(template_id):
                    self.db.set_setting("default_target", f"template:{template_id}")
                await self.show_target_picker(chat_id, message_id, note="✅ Default target updated.")
            elif data == "auto_delete":
                await self.show_auto_delete(chat_id, message_id)
            elif data.startswith("auto:"):
                seconds = int(data.split(":", 1)[1])
                if seconds < 0 or seconds > 48 * 60 * 60 - 120:
                    await self._edit_or_send(chat_id, message_id, "That timer is not supported by Telegram. Choose up to the 48-hour safe cutoff.", self._auto_keyboard())
                else:
                    self.db.set_setting("auto_delete_seconds", seconds)
                    await self.show_auto_delete(chat_id, message_id, note=f"✅ Auto-delete set to {format_duration(seconds)}.")
            elif data == "status":
                await self.show_status(chat_id, message_id)
            else:
                await self.show_panel(chat_id, message_id)
        except (ValueError, KeyError) as exc:
            log.warning("Invalid callback %r: %s", data, exc)
            await self._edit_or_send(chat_id, message_id, "That button is no longer valid. Open /admin and try again.")

    async def show_panel(self, chat_id: int, message_id: int | None = None) -> None:
        target, targets = self.db.resolve_target_channels()
        text = (
            "📣 <b>CHANNEL PUBLISHER — ADMIN PANEL</b>\n\n"
            f"Connected channels: <b>{self.db.active_channel_count()}</b>\n"
            f"Default target: <b>{escape_html(target)}</b> ({len(targets)} channel(s))\n"
            f"Auto-delete: <b>{format_duration(self.db.auto_delete_seconds())}</b>\n\n"
            "Send or forward a post to this bot in private chat to publish it."
        )
        keyboard = [
            [{"text": "📢 Manage channels", "callback_data": "channels"}],
            [{"text": "🧩 Set channel templates", "callback_data": "templates"}],
            [{"text": "🎯 Default target", "callback_data": "target"}],
            [{"text": "⏱ Auto delete", "callback_data": "auto_delete"}],
            [{"text": "📊 Status", "callback_data": "status"}],
        ]
        await self._edit_or_send(chat_id, message_id, text, keyboard, parse_mode="HTML")

    async def show_channels(self, chat_id: int, message_id: int | None, page: int, note: str | None = None) -> None:
        channels = self.db.get_channels(active_only=True)
        page_size = 10
        page = max(0, min(page, max(0, (len(channels) - 1) // page_size)))
        start = page * page_size
        view = channels[start:start + page_size]
        lines = [f"📢 <b>Connected channels ({len(channels)})</b>"]
        if note:
            lines.append(note)
        if not view:
            lines.append("\nNo channels connected yet. Add this bot as an administrator in a channel.")
        else:
            lines.extend(f"\n{start + i + 1}. {escape_html(channel_label(channel))}" for i, channel in enumerate(view))
        keyboard: list[list[dict[str, str]]] = []
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "⬅️ Previous", "callback_data": f"channels:{page - 1}"})
        if start + page_size < len(channels):
            nav.append({"text": "Next ➡️", "callback_data": f"channels:{page + 1}"})
        if nav:
            keyboard.append(nav)
        keyboard.append([{"text": "⬅️ Admin panel", "callback_data": "panel"}])
        await self._edit_or_send(chat_id, message_id, "".join(lines), keyboard, parse_mode="HTML")

    async def show_templates(self, chat_id: int, message_id: int | None, note: str | None = None) -> None:
        templates = self.db.get_templates()
        lines = ["🧩 <b>Channel templates</b>"]
        if note:
            lines.append(note)
        lines.append("\nCreate a template, then add the bot as admin in the channels for that template. You can also select channels that are already connected.")
        if not templates:
            lines.append("\nNo templates created yet.")
        keyboard: list[list[dict[str, str]]] = [[{"text": "➕ Create a template", "callback_data": "template_create"}]]
        default_target = self.db.get_setting("default_target", "all") or "all"
        for template in templates:
            marker = "⭐ " if default_target == f"template:{template['id']}" else ""
            lines.append(f"\n• {escape_html(str(template['name']))} — {template['channel_count']} channel(s)")
            keyboard.append([
                {"text": f"{marker}📂 {template['name'][:24]}", "callback_data": f"template_existing:{template['id']}:0"},
                {"text": "🗑", "callback_data": f"template_delete:{template['id']}"},
            ])
        keyboard.append([{"text": "🎯 Choose default target", "callback_data": "target"}])
        keyboard.append([{"text": "⬅️ Admin panel", "callback_data": "panel"}])
        await self._edit_or_send(chat_id, message_id, "\n".join(lines), keyboard, parse_mode="HTML")

    async def show_template_capture(self, chat_id: int, template_id: int) -> None:
        template = self.db.get_template(template_id)
        if not template:
            await self.api.call("sendMessage", {"chat_id": chat_id, "text": "Template not found."})
            return
        keyboard = [
            [{"text": "➕ Select already-connected channels", "callback_data": f"template_existing:{template_id}:0"}],
            [{"text": "✅ Finish adding channels", "callback_data": f"template_done:{template_id}"}],
            [{"text": "Cancel capture", "callback_data": f"template_cancel:{template_id}"}],
        ]
        await self.api.call("sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"🧩 Template <b>{escape_html(str(template['name']))}</b> created.\n\n"
                "Now add this bot as an <b>administrator</b> in each channel you want in this template. "
                "Newly connected channels will be added to this template automatically.\n\n"
                "You can also choose channels that are already connected. Press <b>Finish adding channels</b> when done."
            ),
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": keyboard},
        })

    async def show_template_channel_picker(
        self, chat_id: int, message_id: int | None, template_id: int, page: int
    ) -> None:
        template = self.db.get_template(template_id)
        if not template:
            await self._edit_or_send(chat_id, message_id, "Template not found.")
            return
        channels = self.db.get_channels(active_only=True)
        page_size = 8
        page = max(0, min(page, max(0, (len(channels) - 1) // page_size)))
        start = page * page_size
        keyboard: list[list[dict[str, str]]] = []
        for channel in channels[start:start + page_size]:
            chat_id_value = int(channel["chat_id"])
            selected = self.db.template_contains(template_id, chat_id_value)
            prefix = "✅ " if selected else "▫️ "
            label = (prefix + str(channel.get("title") or "Channel"))[:32]
            keyboard.append([{
                "text": label,
                "callback_data": f"template_toggle:{template_id}:{chat_id_value}:{page}",
            }])
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "⬅️ Previous", "callback_data": f"template_existing:{template_id}:{page - 1}"})
        if start + page_size < len(channels):
            nav.append({"text": "Next ➡️", "callback_data": f"template_existing:{template_id}:{page + 1}"})
        if nav:
            keyboard.append(nav)
        keyboard.append([{"text": "✅ Finish adding", "callback_data": f"template_done:{template_id}"}])
        keyboard.append([{"text": "⬅️ Templates", "callback_data": "templates"}])
        selected_count = len(self.db.get_template_channels(template_id, active_only=False))
        text = (
            f"🧩 <b>{escape_html(str(template['name']))}</b>\n"
            f"Selected channels: <b>{selected_count}</b>\n\n"
            "Tap a channel to add/remove it from this template."
        )
        await self._edit_or_send(chat_id, message_id, text, keyboard, parse_mode="HTML")

    async def show_target_picker(self, chat_id: int, message_id: int | None, note: str | None = None) -> None:
        templates = self.db.get_templates()
        current = self.db.get_setting("default_target", "all") or "all"
        lines = ["🎯 <b>Default publishing target</b>"]
        if note:
            lines.append(note)
        lines.append("\nEvery new post sent to the bot will be published to the selected target.")
        keyboard: list[list[dict[str, str]]] = []
        keyboard.append([{
            "text": ("✅ " if current == "all" else "") + "All connected channels",
            "callback_data": "target_all",
        }])
        for template in templates:
            selected = current == f"template:{template['id']}"
            keyboard.append([{
                "text": ("✅ " if selected else "") + f"{template['name']} ({template['channel_count']})",
                "callback_data": f"target_template:{template['id']}",
            }])
        keyboard.append([{"text": "⬅️ Admin panel", "callback_data": "panel"}])
        await self._edit_or_send(chat_id, message_id, "\n".join(lines), keyboard, parse_mode="HTML")

    async def show_auto_delete(self, chat_id: int, message_id: int | None, note: str | None = None) -> None:
        current = self.db.auto_delete_seconds()
        lines = ["⏱ <b>Auto-delete timer</b>"]
        if note:
            lines.append(note)
        lines.append(
            "\nChoose how long posts published by this bot remain in each channel. "
            "The timer is saved in SQLite and survives bot restarts.\n\n"
            "<b>Telegram limit:</b> the Bot API cannot delete messages after 48 hours. "
            "Therefore 3–6 day timers are not available; the 48-hour option uses a small safety margin."
        )
        keyboard: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for label, seconds in DELETE_OPTIONS:
            mark = "✅ " if seconds == current else ""
            row.append({"text": mark + label, "callback_data": f"auto:{seconds}"})
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([{"text": "⬅️ Admin panel", "callback_data": "panel"}])
        await self._edit_or_send(chat_id, message_id, "\n".join(lines), keyboard, parse_mode="HTML")

    def _auto_keyboard(self) -> list[list[dict[str, str]]]:
        return [[{"text": label, "callback_data": f"auto:{seconds}"}] for label, seconds in DELETE_OPTIONS]

    async def show_status(self, chat_id: int, message_id: int | None) -> None:
        target, targets = self.db.resolve_target_channels()
        text = (
            "📊 <b>Bot status</b>\n\n"
            f"Connected channels: {self.db.active_channel_count()}\n"
            f"Templates: {len(self.db.get_templates())}\n"
            f"Default target: {escape_html(target)} ({len(targets)} active channel(s))\n"
            f"Auto-delete: {format_duration(self.db.auto_delete_seconds())}\n"
            f"Pending auto-deletions: {self.db.pending_delete_count()}\n"
            f"Bot username: @{self.bot_username or 'loading'}"
        )
        await self._edit_or_send(chat_id, message_id, text, [[{"text": "⬅️ Admin panel", "callback_data": "panel"}]], parse_mode="HTML")

    # ------------------------------------------------------------------
    # Persistent auto-delete worker
    # ------------------------------------------------------------------
    async def _deletion_worker(self) -> None:
        while not self.stopping:
            try:
                now = int(time.time())
                jobs = self.db.get_due_deletions(now)
                for job in jobs:
                    try:
                        await self.api.call_with_retry("deleteMessage", {
                            "chat_id": int(job["chat_id"]),
                            "message_id": int(job["message_id"]),
                        })
                        self.db.complete_delete(int(job["id"]))
                        log.info("Auto-deleted message %s in channel %s", job["message_id"], job["chat_id"])
                    except TelegramAPIError as exc:
                        description = exc.description.lower()
                        if exc.error_code == 400 and any(
                            phrase in description
                            for phrase in ("message to delete not found", "message can't be deleted", "message_id_invalid")
                        ):
                            self.db.complete_delete(int(job["id"]))
                            log.warning("Auto-delete job completed without deletion: %s", exc.description)
                        else:
                            retry_in = (exc.retry_after + 2) if exc.retry_after else 300
                            self.db.retry_delete(int(job["id"]), int(time.time()) + retry_in, exc.description)
                            log.error("Auto-delete will retry later: %s", exc)
                    except Exception as exc:
                        self.db.retry_delete(int(job["id"]), int(time.time()) + 300, str(exc))
                        log.exception("Auto-delete worker error")
                    if self.settings.send_delay_seconds:
                        await asyncio.sleep(self.settings.send_delay_seconds)
            except Exception:
                log.exception("Auto-delete worker loop failed")
            await asyncio.sleep(self.settings.delete_check_interval)

    # ------------------------------------------------------------------
    # Small API helpers
    # ------------------------------------------------------------------
    async def _edit_or_send(
        self,
        chat_id: int,
        message_id: int | None,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        elif message_id is not None:
            payload["reply_markup"] = {"inline_keyboard": []}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if message_id is not None:
            payload["message_id"] = message_id
            try:
                await self.api.call("editMessageText", payload)
                return
            except TelegramAPIError as exc:
                if "message is not modified" in exc.description.lower():
                    return
                log.debug("editMessageText fallback to sendMessage: %s", exc)
        payload.pop("message_id", None)
        await self.api.call("sendMessage", payload)

    async def _notify_owners(self, text: str) -> None:
        for owner_id in self.settings.owner_ids:
            try:
                await self.api.call("sendMessage", {"chat_id": owner_id, "text": text})
            except Exception as exc:
                # Telegram bots can only DM users who have opened the bot first.
                log.debug("Could not DM owner %s (they may not have pressed Start): %s", owner_id, exc)
