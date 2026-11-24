import asyncio
import tempfile
import unittest
from pathlib import Path

from bot.app import ChannelPublisherBot
from bot.config import Settings


class FakeTelegramAPI:
    def __init__(self):
        self.calls = []
        self.copy_calls = []
        self.next_message_id = 100

    async def call(self, method, payload=None):
        payload = payload or {}
        self.calls.append((method, payload))
        if method == "sendMessage":
            return {"message_id": 50}
        if method == "editMessageText":
            return True
        if method == "deleteMessage":
            return True
        return True

    async def call_with_retry(self, method, payload, attempts=5):
        self.calls.append((method, payload))
        if method == "copyMessage":
            self.copy_calls.append(payload)
            self.next_message_id += 1
            return {"message_id": self.next_message_id}
        return True

    async def close(self):
        pass


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = Settings(
            bot_token="test-token",
            owner_ids=frozenset({12345}),
            admin_ids=frozenset(),
            database_path=Path(self.temp_dir.name) / "bot.db",
            poll_timeout=10,
            send_delay_seconds=0,
            delete_check_interval=10,
        )
        self.app = ChannelPublisherBot(settings)
        self.fake_api = FakeTelegramAPI()
        self.app.api = self.fake_api

    def tearDown(self):
        self.app.db.close()
        self.temp_dir.cleanup()

    def test_publish_copies_button_markup_to_every_connected_channel(self):
        for channel_id in (-1001, -1002, -1003):
            self.app.db.register_channel(channel_id, f"Channel {channel_id}", None)
        self.app.db.set_setting("auto_delete_seconds", 1800)
        keyboard = {
            "inline_keyboard": [[
                {"text": "Watch", "url": "https://example.com/watch", "style": "success"}
            ]]
        }
        source = {"message_id": 777, "reply_markup": keyboard, "text": "Post body"}

        asyncio.run(self.app.publish_message(source, 12345))

        self.assertEqual(len(self.fake_api.copy_calls), 3)
        self.assertTrue(all(call["reply_markup"] is keyboard for call in self.fake_api.copy_calls))
        self.assertEqual(self.app.db.get_delivered_target_ids(12345, 777), {-1001, -1002, -1003})
        self.assertEqual(self.app.db.pending_delete_count(), 3)

    def test_replayed_source_message_is_not_published_twice(self):
        self.app.db.register_channel(-1001, "Channel", None)
        source = {"message_id": 778, "text": "Post body"}

        asyncio.run(self.app.publish_message(source, 12345))
        asyncio.run(self.app.publish_message(source, 12345))

        self.assertEqual(len(self.fake_api.copy_calls), 1)

    def test_bot_added_as_admin_registers_channel_and_posts_confirmation(self):
        template_id = self.app.db.create_template("New set", 12345)
        self.app.db.set_admin_state(12345, "template_capture", {"template_id": template_id})
        update = {
            "chat": {"id": -100987, "type": "channel", "title": "New channel", "username": "new_channel"},
            "from": {"id": 12345},
            "old_chat_member": {"status": "left"},
            "new_chat_member": {"status": "administrator"},
        }

        asyncio.run(self.app.handle_my_chat_member(update))

        self.assertEqual(self.app.db.active_channel_count(), 1)
        self.assertEqual(
            [c["chat_id"] for c in self.app.db.get_template_channels(template_id)],
            [-100987],
        )
        self.assertTrue(any(
            method == "sendMessage" and payload.get("chat_id") == -100987
            and "Bot added successfully" in payload.get("text", "")
            for method, payload in self.fake_api.calls
        ))


if __name__ == "__main__":
    unittest.main()
