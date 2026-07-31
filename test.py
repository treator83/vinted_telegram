"""Automated tests for Vinted Agent core functionality."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from database import Database
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
        listing = make_listing(title="Alpinestars Motorcycle Boots")

        self.assertFalse(allow(listing, self.search))

    def test_maximum_price_filter_rejects_expensive_listing(self) -> None:
        listing = make_listing(price="£80.01")

        self.assertFalse(allow(listing, self.search))

    def test_maximum_price_is_inclusive(self) -> None:
        listing = make_listing(price="£80.00")

        self.assertTrue(allow(listing, self.search))

    def test_size_filter_rejects_wrong_size(self) -> None:
        listing = make_listing(subtitle="Size 8 · Very Good")

        self.assertFalse(allow(listing, self.search))

    def test_size_filter_does_not_match_partial_number(self) -> None:
        listing = make_listing(subtitle="Size 19 · Very Good")

        self.assertFalse(allow(listing, self.search))

    def test_condition_filter_rejects_wrong_condition(self) -> None:
        listing = make_listing(subtitle="Size 9 · Satisfactory")

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
        self.addCleanup(self.temporary_directory.cleanup)

        self.searches_file = (
            Path(self.temporary_directory.name) / "searches.json"
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

        searches = SearchManager(self.searches_file).load()

        self.assertEqual(len(searches), 1)
        self.assertEqual(searches[0].id, "rst_boots")
        self.assertEqual(searches[0].name, "RST Boots")
        self.assertEqual(searches[0].max_price, 80.0)
        self.assertEqual(searches[0].sizes, ["9", "10"])

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

        search = SearchManager(self.searches_file).load()[0]

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

        with self.assertRaises(SearchConfigurationError):
            SearchManager(self.searches_file).load()

    def test_invalid_json_is_rejected(self) -> None:
        self.searches_file.write_text(
            "{invalid json",
            encoding="utf-8",
        )

        with self.assertRaises(SearchConfigurationError):
            SearchManager(self.searches_file).load()

    def test_missing_required_field_is_rejected(self) -> None:
        self._write_json(
            [
                {
                    "id": "missing_url",
                    "name": "Missing URL",
                }
            ]
        )

        with self.assertRaises(SearchConfigurationError):
            SearchManager(self.searches_file).load()

    def _write_json(self, data: object) -> None:
        self.searches_file.write_text(
            json.dumps(data),
            encoding="utf-8",
        )


class DatabaseTests(unittest.TestCase):
    """Test SQLite listing persistence and price history."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "listings.db"
        )
        self.database = Database(self.database_path)

    def tearDown(self) -> None:
        self.database.close()
        self.temporary_directory.cleanup()

    def test_saves_and_reads_listing(self) -> None:
        listing = make_listing()

        inserted = self.database.save(listing)
        stored = self.database.get(listing.id)

        self.assertTrue(inserted)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["id"], listing.id)
        self.assertEqual(stored["title"], listing.title)
        self.assertEqual(stored["current_price"], 50.0)
        self.assertEqual(self.database.count(), 1)

    def test_duplicate_listing_is_not_inserted_twice(self) -> None:
        listing = make_listing()

        self.assertTrue(self.database.save(listing))
        self.assertFalse(self.database.save(listing))
        self.assertEqual(self.database.count(), 1)

    def test_exists_detects_stored_listing(self) -> None:
        listing = make_listing()
        self.database.save(listing)

        self.assertTrue(self.database.exists(listing.id))
        self.assertFalse(self.database.exists("not-present"))

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
            self.database.count_for_search("RST Boots"),
            2,
        )
        self.assertEqual(
            self.database.count_for_search("Alpinestars Boots"),
            1,
        )

    def test_updates_price_and_records_history(self) -> None:
        original = make_listing(price="£50.00")
        updated = make_listing(price="£40.00")

        self.database.save(original)
        price_changed = self.database.update_price(updated)

        stored = self.database.get(updated.id)
        history = self.database.price_history(updated.id)

        self.assertTrue(price_changed)
        self.assertEqual(stored["previous_price"], 50.0)
        self.assertEqual(stored["current_price"], 40.0)
        self.assertEqual(
            [row["price"] for row in history],
            [50.0, 40.0],
        )

    def test_unchanged_price_does_not_add_history_entry(self) -> None:
        listing = make_listing(price="£50.00")

        self.database.save(listing)
        price_changed = self.database.update_price(listing)

        history = self.database.price_history(listing.id)

        self.assertFalse(price_changed)
        self.assertEqual(len(history), 1)

    def test_clear_removes_all_data(self) -> None:
        listing = make_listing()
        self.database.save(listing)

        self.database.clear()

        self.assertEqual(self.database.count(), 0)
        self.assertEqual(self.database.price_history(listing.id), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)