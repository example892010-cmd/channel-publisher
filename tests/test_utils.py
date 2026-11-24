import unittest

from bot.utils import (
    DELETE_OPTIONS,
    MAX_SUPPORTED_DELETE_SECONDS,
    build_copy_payload,
    format_duration,
    get_delete_due_at,
)


class UtilityTests(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(0), "Off")
        self.assertEqual(format_duration(1800), "30 minute(s)")
        self.assertEqual(format_duration(3600), "1 hour(s)")
        self.assertEqual(format_duration(86400), "1 day(s)")

    def test_due_at(self):
        self.assertIsNone(get_delete_due_at(1000, 0))
        self.assertEqual(get_delete_due_at(1000, 1800), 2800)
        with self.assertRaises(ValueError):
            get_delete_due_at(1000, MAX_SUPPORTED_DELETE_SECONDS + 1)

    def test_auto_delete_options_stay_inside_bot_api_limit(self):
        self.assertEqual(DELETE_OPTIONS[0], ("Off", 0))
        self.assertEqual(DELETE_OPTIONS[-1][1], MAX_SUPPORTED_DELETE_SECONDS)
        self.assertLess(DELETE_OPTIONS[-1][1], 48 * 60 * 60)

    def test_copy_payload_preserves_raw_inline_keyboard(self):
        keyboard = {
            "inline_keyboard": [[
                {"text": "Open", "url": "https://example.com", "style": "success"},
                {"text": "Action", "callback_data": "abc", "style": "danger"},
            ]]
        }
        source = {"message_id": 42, "reply_markup": keyboard, "text": "Hello"}
        payload = build_copy_payload(-100123, 987654, source)
        self.assertEqual(payload["chat_id"], -100123)
        self.assertEqual(payload["from_chat_id"], 987654)
        self.assertEqual(payload["message_id"], 42)
        self.assertIs(payload["reply_markup"], keyboard)

    def test_copy_payload_does_not_invent_keyboard(self):
        payload = build_copy_payload(-100123, 987654, {"message_id": 10, "text": "Only text"})
        self.assertNotIn("reply_markup", payload)


if __name__ == "__main__":
    unittest.main()
