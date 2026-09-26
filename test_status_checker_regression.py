"""Regression tests for bounded Vinted item-detail parsing."""

from __future__ import annotations

import time
import unittest

from status_checker import ListingStatusChecker


class StatusCheckerRegressionTests(unittest.TestCase):
    """Guard against CPU hangs while parsing escaped Next.js payloads."""

    def test_large_malformed_description_is_bounded(self) -> None:
        payload = (
            '{"description":"'
            + ("\\\\" * 100_000)
            + '","different_key":false}'
        )

        started = time.monotonic()

        value = ListingStatusChecker._json_string_value(
            payload,
            "description",
            following_key="is_expanded",
        )

        elapsed = time.monotonic() - started

        self.assertIsNone(value)
        self.assertLess(
            elapsed,
            2.0,
            "description parsing took too long",
        )

    def test_escaped_quotes_do_not_terminate_value_early(self) -> None:
        payload = (
            r'{"description":"He said \\\"hello\\\" today",'
            r'"is_expanded":false}'
        )

        value = ListingStatusChecker._json_string_value(
            payload,
            "description",
            following_key="is_expanded",
        )

        self.assertEqual(
            value,
            r'He said \"hello\" today',
        )

    def test_malformed_attribute_value_is_bounded(self) -> None:
        payload = (
            '{"code":"brand","data":{"value":"'
            + ("\\\\" * 100_000)
        )

        started = time.monotonic()

        value = ListingStatusChecker._attribute_value(
            payload,
            "brand",
        )

        elapsed = time.monotonic() - started

        self.assertIsNone(value)
        self.assertLess(
            elapsed,
            2.0,
            "attribute parsing took too long",
        )


if __name__ == "__main__":
    unittest.main()
