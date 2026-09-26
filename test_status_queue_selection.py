"""Tests for selecting only current-search listings for status checks."""

from __future__ import annotations

import unittest

from main import VintedAgent
from search import Search


class StatusQueueSelectionTests(unittest.TestCase):
    """Ensure historical and newly-filtered rows do not use browser checks."""

    @staticmethod
    def _row(
        *,
        listing_id: str,
        search_id: str,
        title: str,
        subtitle: str,
        price: float,
        size: str | None = None,
    ) -> dict[str, object]:
        return {
            "id": listing_id,
            "search_id": search_id,
            "search_name": search_id,
            "title": title,
            "subtitle": subtitle,
            "size": size,
            "item_condition": None,
            "brand": None,
            "description": None,
            "current_price": price,
            "url": f"https://www.vinted.co.uk/items/{listing_id}",
            "image": "https://example.com/image.jpg",
            "posted_at": None,
            "local_image_path": None,
        }

    def setUp(self) -> None:
        self.motorcycle = Search(
            id="motorcycle_boots",
            name="Motorcycle Boots",
            url="https://www.vinted.co.uk/catalog?search_text=motorbike%20boots",
            max_price=20,
        )

        self.climbing = Search(
            id="climbing_shoes",
            name="Climbing Shoes",
            url="https://www.vinted.co.uk/catalog?search_text=bouldering%20climbing%20shoes",
            max_price=20,
            keywords=["scarpa", "la sportiva"],
            sizes=["4+"],
        )

    def test_removed_search_is_ignored(self) -> None:
        candidates = [
            self._row(
                listing_id="1",
                search_id="rst_boots",
                title="RST motorcycle boots",
                subtitle="Size 9",
                price=15,
            ),
            self._row(
                listing_id="2",
                search_id="motorcycle_boots",
                title="Motorbike boots",
                subtitle="Size 9",
                price=15,
            ),
        ]

        selected = VintedAgent._select_status_check_listings(
            candidates,
            [self.motorcycle, self.climbing],
            limit=10,
        )

        self.assertEqual(
            [row["id"] for row in selected],
            ["2"],
        )

    def test_old_irrelevant_climbing_listing_is_ignored(self) -> None:
        candidates = [
            self._row(
                listing_id="1",
                search_id="climbing_shoes",
                title="Extendable shoe horn",
                subtitle="One size",
                price=5,
            ),
            self._row(
                listing_id="2",
                search_id="climbing_shoes",
                title="Scarpa climbing shoes",
                subtitle="UK 7 · Good",
                price=18,
            ),
        ]

        selected = VintedAgent._select_status_check_listings(
            candidates,
            [self.motorcycle, self.climbing],
            limit=10,
        )

        self.assertEqual(
            [row["id"] for row in selected],
            ["2"],
        )

    def test_current_price_filter_is_reapplied(self) -> None:
        candidates = [
            self._row(
                listing_id="1",
                search_id="motorcycle_boots",
                title="Motorbike boots",
                subtitle="Size 8",
                price=25,
            ),
            self._row(
                listing_id="2",
                search_id="motorcycle_boots",
                title="Motorbike boots",
                subtitle="Size 8",
                price=20,
            ),
        ]

        selected = VintedAgent._select_status_check_listings(
            candidates,
            [self.motorcycle],
            limit=10,
        )

        self.assertEqual(
            [row["id"] for row in selected],
            ["2"],
        )

    def test_selection_respects_batch_limit(self) -> None:
        candidates = [
            self._row(
                listing_id=str(index),
                search_id="motorcycle_boots",
                title="Motorbike boots",
                subtitle="Size 9",
                price=10,
            )
            for index in range(20)
        ]

        selected = VintedAgent._select_status_check_listings(
            candidates,
            [self.motorcycle],
            limit=3,
        )

        self.assertEqual(len(selected), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
