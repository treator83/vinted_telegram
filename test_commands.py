"""Tests for Telegram command helpers."""

from __future__ import annotations

import unittest

from telegram_commands import TelegramCommandBot


class TelegramCommandTests(unittest.TestCase):
    """Test command parsing and static responses without network access."""

    def test_normalises_plain_command(self) -> None:
        self.assertEqual(
            TelegramCommandBot._normalise_command(
                "  /STATS  "
            ),
            "/stats",
        )

    def test_normalises_bot_mention(self) -> None:
        self.assertEqual(
            TelegramCommandBot._normalise_command(
                "/report@MyVintedBot now"
            ),
            "/report",
        )

    def test_help_lists_core_commands(self) -> None:
        message = TelegramCommandBot._help_message()

        for command in (
            "/stats",
            "/report",
            "/sold",
            "/new",
            "/searches",
            "/health",
            "/pause",
            "/resume",
            "/status",
            "/schedule",
        ):
            self.assertIn(
                command,
                message,
            )

    def test_truncate_leaves_short_message_unchanged(self) -> None:
        self.assertEqual(
            TelegramCommandBot._truncate(
                "hello",
                10,
            ),
            "hello",
        )

    def test_truncate_limits_long_message(self) -> None:
        result = TelegramCommandBot._truncate(
            "abcdefghijk",
            8,
        )

        self.assertLessEqual(
            len(result),
            8,
        )
        self.assertTrue(
            result.endswith("…")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
