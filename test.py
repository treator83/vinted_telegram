"""Automated tests for Vinted Agent core functionality."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from database import Database, SCHEMA_VERSION
from filters import allow
from models import Listing, extract_price
from search import Search
from search_manager import SearchConfigurationError, SearchManager


def make_listing(
    *,
    listing_id: str = "123456",
    title: str = "RST Motorcycle Boots",
    subtitle: str = "Size 9 · Very Good",
    price: str = "£50.00",
    total_price: str = "£53.70",
    search_id: str = "rst_boots",
    search_name: str = "RST Boots",
    size: str | None = None,
    condition: str | None = None,
    brand: str | None = None,
    description: str | None = None,
    posted_at: str | None = None,
) -> Listing:
    """Create a listing suitable for tests."""

    return Listing(
        id=listing_id,
        title=title,
        subtitle=subtitle,
        price=price,
        total_price=total_price,
        url=f"https://www.vinted.co.uk/items/{listing_id}",
        image="https://example.com/image.jpg",
        search_id=search_id,
        search_name=search_name,
        size=size,
        condition=condition,
        brand=brand,
        description=description,
        posted_at=posted_at,
    )


class PriceParsingTests(unittest.TestCase):
    """Test conversion of display prices into numeric values."""

    def test_extracts_decimal_price(self) -> None:
        self.assertEqual(extract_price("£74.20"), 74.20)

    def test_extracts_comma_decimal_price(self) -> None:
        self.assertEqual(extract_price("€99,95"), 99.95)

    def test_extracts_integer_price(self) -> None:
        self.assertEqual(extract_price("£80"), 80.0)

    def test_empty_price_returns_zero(self) -> None:
        self.assertEqual(extract_price(""), 0.0)
        self.assertEqual(extract_price(None), 0.0)

    def test_listing_calculates_numeric_prices(self) -> None:
        listing = make_listing(
            price="£64.99",
            total_price="£69.14",
        )

        self.assertEqual(listing.price_value, 64.99)
        self.assertEqual(listing.total_price_value, 69.14)

    def test_listing_normalises_picture_list(self) -> None:
        listing = Listing(
            id="1",
            title="Boots",
            subtitle="",
            price="£20",
            total_price="£22",
            url="https://example.com/item",
            image="https://example.com/main.jpg",
            pictures=[
                "https://example.com/extra.jpg",
                "https://example.com/extra.jpg",
            ],
        )

        self.assertEqual(
            listing.pictures,
            [
                "https://example.com/main.jpg",
                "https://example.com/extra.jpg",
            ],
        )


class FilterTests(unittest.TestCase):
    """Test search-specific listing filters."""

    def setUp(self) -> None:
        self.search = Search(
            id="rst_boots",
            name="RST Boots",
            url="https://www.vinted.co.uk/catalog?search_text=rst",
            max_price=80,
            keywords=["rst"],
            sizes=["9", "10"],
            conditions=["Very Good", "Good"],
        )

    def test_matching_listing_is_allowed(self) -> None:
        listing = make_listing()
        self.assertTrue(allow(listing, self.search))

    def test_keyword_filter_rejects_non_matching_listing(self) -> None:
        listing = make_listing(
            title="Alpinestars Motorcycle Boots"
        )
        self.assertFalse(allow(listing, self.search))

    def test_maximum_price_filter_rejects_expensive_listing(self) -> None:
        listing = make_listing(price="£80.01")
        self.assertFalse(allow(listing, self.search))

    def test_maximum_price_is_inclusive(self) -> None:
        listing = make_listing(price="£80.00")
        self.assertTrue(allow(listing, self.search))

    def test_size_filter_rejects_wrong_size(self) -> None:
        listing = make_listing(
            subtitle="Size 8 · Very Good"
        )
        self.assertFalse(allow(listing, self.search))

    def test_size_filter_does_not_match_partial_number(self) -> None:
        listing = make_listing(
            subtitle="Size 19 · Very Good"
        )
        self.assertFalse(allow(listing, self.search))

    def test_condition_filter_rejects_wrong_condition(self) -> None:
        listing = make_listing(
            subtitle="Size 9 · Satisfactory"
        )
        self.assertFalse(allow(listing, self.search))

    def test_search_without_filters_allows_listing(self) -> None:
        search = Search(
            id="all_items",
            name="All Items",
            url="https://www.vinted.co.uk/catalog",
        )

        listing = make_listing(
            title="Any brand",
            subtitle="Any size · Any condition",
            price="£999.00",
        )

        self.assertTrue(allow(listing, search))


class SearchManagerTests(unittest.TestCase):
    """Test loading and validation of searches.json."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(
            self.temporary_directory.cleanup
        )

        self.searches_file = (
            Path(self.temporary_directory.name)
            / "searches.json"
        )

    def test_loads_valid_search_configuration(self) -> None:
        self._write_json(
            [
                {
                    "id": "rst_boots",
                    "name": "RST Boots",
                    "url": "https://www.vinted.co.uk/catalog?search_text=rst",
                    "max_price": 80,
                    "keywords": ["rst"],
                    "sizes": ["9", "10"],
                    "conditions": ["Very Good", "Good"],
                }
            ]
        )

        searches = SearchManager(
            self.searches_file
        ).load()

        self.assertEqual(len(searches), 1)
        self.assertEqual(
            searches[0].id,
            "rst_boots",
        )
        self.assertEqual(
            searches[0].name,
            "RST Boots",
        )
        self.assertEqual(
            searches[0].max_price,
            80.0,
        )
        self.assertEqual(
            searches[0].sizes,
            ["9", "10"],
        )

    def test_optional_filters_default_to_empty_values(self) -> None:
        self._write_json(
            [
                {
                    "id": "all_items",
                    "name": "All Items",
                    "url": "https://www.vinted.co.uk/catalog",
                }
            ]
        )

        search = SearchManager(
            self.searches_file
        ).load()[0]

        self.assertIsNone(search.max_price)
        self.assertEqual(search.keywords, [])
        self.assertEqual(search.sizes, [])
        self.assertEqual(search.conditions, [])

    def test_duplicate_search_ids_are_rejected(self) -> None:
        self._write_json(
            [
                {
                    "id": "duplicate",
                    "name": "First Search",
                    "url": "https://www.vinted.co.uk/catalog?search_text=first",
                },
                {
                    "id": "duplicate",
                    "name": "Second Search",
                    "url": "https://www.vinted.co.uk/catalog?search_text=second",
                },
            ]
        )

        with self.assertRaises(
            SearchConfigurationError
        ):
            SearchManager(
                self.searches_file
            ).load()

    def test_invalid_json_is_rejected(self) -> None:
        self.searches_file.write_text(
            "{invalid json",
            encoding="utf-8",
        )

        with self.assertRaises(
            SearchConfigurationError
        ):
            SearchManager(
                self.searches_file
            ).load()

    def test_missing_required_field_is_rejected(self) -> None:
        self._write_json(
            [
                {
                    "id": "missing_url",
                    "name": "Missing URL",
                }
            ]
        )

        with self.assertRaises(
            SearchConfigurationError
        ):
            SearchManager(
                self.searches_file
            ).load()

    def _write_json(self, data: object) -> None:
        self.searches_file.write_text(
            json.dumps(data),
            encoding="utf-8",
        )


class DatabaseTests(unittest.TestCase):
    """Test SQLite listing persistence and price history."""

    def setUp(self) -> None:
        self.temporary_directory = (
            tempfile.TemporaryDirectory()
        )

        self.database_path = (
            Path(self.temporary_directory.name)
            / "listings.db"
        )

        self.database = Database(
            self.database_path
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temporary_directory.cleanup()

    def test_schema_version_is_current(self) -> None:
        version = self.database.connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0]

        self.assertEqual(
            version,
            SCHEMA_VERSION,
        )
        self.assertEqual(
            SCHEMA_VERSION,
            5,
        )

    def test_saves_and_reads_listing(self) -> None:
        listing = make_listing(
            size="9",
            condition="Very Good",
            brand="RST",
            description="Motorcycle boots",
            posted_at="2026-09-10 10:00:00",
        )

        inserted = self.database.save(
            listing,
            search_config={
                "id": "rst_boots",
                "max_price": 40,
            },
        )

        stored = self.database.get(
            listing.id
        )

        self.assertTrue(inserted)
        self.assertIsNotNone(stored)
        self.assertEqual(
            stored["id"],
            listing.id,
        )
        self.assertEqual(
            stored["title"],
            listing.title,
        )
        self.assertEqual(
            stored["current_price"],
            50.0,
        )
        self.assertEqual(
            stored["size"],
            "9",
        )
        self.assertEqual(
            stored["item_condition"],
            "Very Good",
        )
        self.assertEqual(
            stored["brand"],
            "RST",
        )
        self.assertEqual(
            stored["description"],
            "Motorcycle boots",
        )
        self.assertEqual(
            stored["posted_at"],
            "2026-09-10 10:00:00",
        )
        self.assertIsNotNone(
            stored["search_config_hash"]
        )
        self.assertEqual(
            self.database.count(),
            1,
        )

    def test_duplicate_listing_is_not_inserted_twice(self) -> None:
        listing = make_listing()

        self.assertTrue(
            self.database.save(listing)
        )
        self.assertFalse(
            self.database.save(listing)
        )
        self.assertEqual(
            self.database.count(),
            1,
        )

    def test_exists_detects_stored_listing(self) -> None:
        listing = make_listing()
        self.database.save(listing)

        self.assertTrue(
            self.database.exists(listing.id)
        )
        self.assertFalse(
            self.database.exists("not-present")
        )

    def test_count_for_search(self) -> None:
        first = make_listing(
            listing_id="1",
            search_name="RST Boots",
        )
        second = make_listing(
            listing_id="2",
            search_name="RST Boots",
        )
        third = make_listing(
            listing_id="3",
            search_id="alpinestars_boots",
            search_name="Alpinestars Boots",
        )

        self.database.save(first)
        self.database.save(second)
        self.database.save(third)

        self.assertEqual(
            self.database.count_for_search(
                "RST Boots"
            ),
            2,
        )
        self.assertEqual(
            self.database.count_for_search(
                "Alpinestars Boots"
            ),
            1,
        )

    def test_updates_price_and_records_history(self) -> None:
        original = make_listing(
            price="£50.00"
        )
        updated = make_listing(
            price="£40.00"
        )

        self.database.save(original)

        price_changed = (
            self.database.update_price(
                updated
            )
        )

        stored = self.database.get(
            updated.id
        )
        history = self.database.price_history(
            updated.id
        )

        self.assertTrue(price_changed)
        self.assertEqual(
            stored["previous_price"],
            50.0,
        )
        self.assertEqual(
            stored["current_price"],
            40.0,
        )
        self.assertEqual(
            [
                row["price"]
                for row in history
            ],
            [50.0, 40.0],
        )

    def test_unchanged_price_does_not_add_history_entry(self) -> None:
        listing = make_listing(
            price="£50.00"
        )

        self.database.save(listing)

        price_changed = (
            self.database.update_price(
                listing
            )
        )

        history = self.database.price_history(
            listing.id
        )

        self.assertFalse(price_changed)
        self.assertEqual(
            len(history),
            1,
        )

    def test_updates_enriched_listing_details(self) -> None:
        listing = make_listing()
        self.database.save(listing)

        updated = (
            self.database
            .update_listing_details(
                listing.id,
                size="10",
                condition="Good",
                brand="RST",
                description="Used boots",
                pictures=[
                    "https://example.com/main.jpg",
                    "https://example.com/second.jpg",
                ],
                posted_at="2026-09-11 15:00:00",
                local_image_path="data/images/123456.jpg",
            )
        )

        stored = self.database.get(
            listing.id
        )

        self.assertTrue(updated)
        self.assertEqual(
            stored["size"],
            "10",
        )
        self.assertEqual(
            stored["item_condition"],
            "Good",
        )
        self.assertEqual(
            stored["brand"],
            "RST",
        )
        self.assertEqual(
            stored["description"],
            "Used boots",
        )
        self.assertEqual(
            stored["posted_at"],
            "2026-09-11 15:00:00",
        )
        self.assertEqual(
            stored["local_image_path"],
            "data/images/123456.jpg",
        )

        pictures = json.loads(
            stored["pictures_json"]
        )

        self.assertIn(
            "https://example.com/second.jpg",
            pictures,
        )

    def test_touch_reactivates_listing(self) -> None:
        listing = make_listing()
        self.database.save(listing)

        self.database.set_listing_status(
            listing.id,
            "sold",
            reason="explicit_sold",
        )

        self.database.touch(
            listing.id
        )

        stored = self.database.get(
            listing.id
        )

        self.assertEqual(
            stored["listing_status"],
            "active",
        )
        self.assertEqual(
            stored["status_reason"],
            "catalogue",
        )
        self.assertIsNone(
            stored["sold_at"]
        )

    def test_status_reason_is_stored(self) -> None:
        listing = make_listing()
        self.database.save(listing)

        changed = (
            self.database
            .set_listing_status(
                listing.id,
                "not_found",
                reason="http_404",
            )
        )

        stored = self.database.get(
            listing.id
        )

        self.assertTrue(changed)
        self.assertEqual(
            stored["listing_status"],
            "not_found",
        )
        self.assertEqual(
            stored["status_reason"],
            "http_404",
        )
        self.assertIsNotNone(
            stored["sold_at"]
        )

    def test_notification_queue_lifecycle(self) -> None:
        listing = make_listing()
        self.database.save(listing)

        notification_id = (
            self.database
            .enqueue_notification(
                listing.id,
                "new_listing",
            )
        )

        pending = (
            self.database
            .pending_notifications()
        )

        self.assertEqual(
            len(pending),
            1,
        )
        self.assertEqual(
            pending[0]["id"],
            notification_id,
        )
        self.assertEqual(
            pending[0]["state"],
            "pending",
        )

        self.database.mark_notification_failed(
            notification_id,
            "temporary error",
        )

        failed = (
            self.database
            .pending_notifications()
        )

        self.assertEqual(
            failed[0]["state"],
            "failed",
        )
        self.assertEqual(
            failed[0]["attempts"],
            1,
        )

        self.database.mark_notification_sent(
            notification_id
        )

        self.assertEqual(
            self.database.pending_notifications(),
            [],
        )

        stored = self.database.get(
            listing.id
        )

        self.assertIsNotNone(
            stored["notified_at"]
        )

    def test_clear_removes_all_data(self) -> None:
        listing = make_listing()
        self.database.save(listing)

        self.database.enqueue_notification(
            listing.id,
            "new_listing",
        )

        self.database.clear()

        self.assertEqual(
            self.database.count(),
            0,
        )
        self.assertEqual(
            self.database.price_history(
                listing.id
            ),
            [],
        )
        self.assertEqual(
            self.database.pending_notifications(),
            [],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
