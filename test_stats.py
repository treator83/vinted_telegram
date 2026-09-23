"""Tests for the richer Vinted statistics report."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import hourly_stats


class RichStatisticsTests(unittest.TestCase):
    """Exercise Phase 5 statistics against a temporary SQLite database."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)

        self.database_path = Path(self.temporary_directory.name) / "listings.db"
        self.original_database_path = hourly_stats.DATABASE_PATH
        hourly_stats.DATABASE_PATH = self.database_path
        self.addCleanup(self._restore_database_path)

        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE listings (
                id TEXT PRIMARY KEY,
                search_id TEXT,
                search_name TEXT,
                title TEXT,
                size TEXT,
                item_condition TEXT,
                current_price REAL,
                url TEXT,
                posted_at TEXT,
                first_seen TEXT,
                listing_status TEXT,
                sold_at TEXT,
                status_checked_at TEXT
            );
            """
        )

        now = datetime.now(timezone.utc).replace(microsecond=0)
        timestamp = lambda value: value.strftime("%Y-%m-%d %H:%M:%S")

        rows = [
            (
                "1",
                "rst",
                "RST Boots",
                "Active boots",
                "9",
                "Good",
                30.0,
                "https://example.com/1",
                timestamp(now - timedelta(days=2)),
                timestamp(now),
                "active",
                None,
                timestamp(now),
            ),
            (
                "2",
                "rst",
                "RST Boots",
                "Fast sold boots",
                "9",
                "Good",
                40.0,
                "https://example.com/2",
                timestamp(now - timedelta(days=1)),
                timestamp(now - timedelta(days=1)),
                "sold",
                timestamp(now),
                timestamp(now),
            ),
            (
                "3",
                "climb",
                "Climbing Shoes",
                "Unavailable shoes",
                "10",
                "Satisfactory",
                20.0,
                "https://example.com/3",
                timestamp(now - timedelta(days=5)),
                timestamp(now - timedelta(days=5)),
                "not_found",
                None,
                timestamp(now),
            ),
            (
                "4",
                "climb",
                "Climbing Shoes",
                "Unknown listing",
                None,
                None,
                70.0,
                "https://example.com/4",
                None,
                timestamp(now),
                "unknown",
                None,
                None,
            ),
        ]

        connection.executemany(
            """
            INSERT INTO listings (
                id,
                search_id,
                search_name,
                title,
                size,
                item_condition,
                current_price,
                url,
                posted_at,
                first_seen,
                listing_status,
                sold_at,
                status_checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()
        connection.close()

    def _restore_database_path(self) -> None:
        hourly_stats.DATABASE_PATH = self.original_database_path

    def test_richer_statistics_are_calculated(self) -> None:
        stats = hourly_stats.get_statistics()

        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["sold"], 2)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(stats["sold_last_7_days"], 2)
        self.assertEqual(stats["sold_last_30_days"], 2)
        self.assertAlmostEqual(stats["average_last_asking_price"], 30.0)
        self.assertAlmostEqual(stats["median_last_asking_price"], 30.0)
        self.assertAlmostEqual(stats["average_days_to_sell"], 3.0)
        self.assertAlmostEqual(stats["median_days_to_sell"], 3.0)
        self.assertEqual(stats["size_enriched"], 3)
        self.assertEqual(stats["condition_enriched"], 3)
        self.assertEqual(stats["posted_at_enriched"], 3)
        self.assertEqual(stats["fastest_sales"][0]["title"], "Fast sold boots")
        self.assertEqual(
            [row["label"] for row in stats["price_bands"]],
            ["£0-20", "£20-40", "£60+"],
        )

    def test_report_contains_new_sections_and_fits_telegram(self) -> None:
        message = hourly_stats.build_message(hourly_stats.get_statistics())

        for heading in (
            "PRICE BANDS",
            "BY CONDITION",
            "TOP SIZES BY SAMPLE",
            "FASTEST SOLD/UNAVAILABLE",
            "Median last asking price",
            "Enrichment coverage",
        ):
            self.assertIn(heading, message)

        self.assertNotIn("Avg sold:", message)
        self.assertLessEqual(len(message), 4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
