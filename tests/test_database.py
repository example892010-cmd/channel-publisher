import tempfile
import unittest
from pathlib import Path

from bot.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "test.db")

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def test_more_than_one_hundred_channels_can_be_registered(self):
        for index in range(105):
            self.db.register_channel(-1000000 - index, f"Channel {index}", None)
        self.assertEqual(self.db.active_channel_count(), 105)
        self.assertEqual(len(self.db.get_channels()), 105)

    def test_template_and_default_target(self):
        self.db.register_channel(-1001, "Tamil", "tamil_channel")
        self.db.register_channel(-1002, "Anime", "anime_channel")
        template_id = self.db.create_template("Tamil set", 12345)
        self.db.add_channel_to_template(template_id, -1001)
        self.db.set_setting("default_target", f"template:{template_id}")

        name, channels = self.db.resolve_target_channels()
        self.assertEqual(name, "Tamil set")
        self.assertEqual([item["chat_id"] for item in channels], [-1001])

        self.db.delete_template(template_id)
        self.assertEqual(self.db.get_setting("default_target"), "all")

    def test_broadcast_delivery_is_idempotent_and_schedules_delete(self):
        self.db.record_broadcast_delivery(12345, 7, -1001, 55, 1234567890)
        self.db.record_broadcast_delivery(12345, 7, -1001, 55, 1234567890)
        self.assertEqual(self.db.get_delivered_target_ids(12345, 7), {-1001})
        due = self.db.get_due_deletions(1234567890)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["message_id"], 55)

    def test_scheduled_deletions_persist(self):
        self.db.schedule_delete(-100123, 99, 1234567890)
        due = self.db.get_due_deletions(1234567890)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["message_id"], 99)
        self.db.retry_delete(due[0]["id"], 1234567990, "temporary error")
        self.assertEqual(self.db.get_due_deletions(1234567890), [])
        self.assertEqual(len(self.db.get_due_deletions(1234567990)), 1)
        self.db.complete_delete(due[0]["id"])
        self.assertEqual(self.db.pending_delete_count(), 0)


if __name__ == "__main__":
    unittest.main()
