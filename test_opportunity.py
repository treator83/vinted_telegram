"""Regression tests for market opportunity scoring."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from database import Database
from models import Listing
from opportunity import OpportunityAnalysis, OpportunityScorer
from telegram_client import TelegramClient


def make_listing(
    listing_id: str,
    *,
    search_id: str = "climbing_shoes",
    search_name: str = "Climbing Shoes",
    brand: str | None = "Scarpa",
    size: str | None = "UK 8",
    condition: str | None = "Very Good",
    price: float = 20.0,
) -> Listing:
    return Listing(
        id=listing_id,
        title=f"{brand or 'Unknown'} shoes",
        subtitle=f"{size or ''} {condition or ''}".strip(),
        price=f"£{price:.2f}",
        total_price=f"£{price:.2f}",
        url=f"https://www.vinted.co.uk/items/{listing_id}",
        image="",
        search_id=search_id,
        search_name=search_name,
        size=size,
        condition=condition,
        brand=brand,
    )


class OpportunityScorerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)

        self.database_path = Path(
            self.temporary_directory.name
        ) / "listings.db"
        self.database = Database(self.database_path)
        self.addCleanup(self.database.close)
        self.scorer = OpportunityScorer(self.database_path)

    def add_sold(
        self,
        listing_id: str,
        *,
        search_id: str = "climbing_shoes",
        search_name: str = "Climbing Shoes",
        brand: str | None = "Scarpa",
        size: str | None = "UK 8",
        condition: str | None = "Very Good",
        price: float = 40.0,
    ) -> None:
        listing = make_listing(
            listing_id,
            search_id=search_id,
            search_name=search_name,
            brand=brand,
            size=size,
            condition=condition,
            price=price,
        )
        self.database.save(listing)
        self.database.set_listing_status(
            listing_id,
            "sold",
            reason="test",
        )

    def test_no_sold_history_reports_building_data(self) -> None:
        listing = make_listing("new-1")

        analysis = self.scorer.analyse(listing)

        self.assertIsNotNone(analysis)
        assert analysis is not None
        self.assertIsNone(analysis.score)
        self.assertEqual(analysis.label, "building_data")
        self.assertEqual(analysis.comparable_count, 0)

    def test_brand_history_can_cross_old_search_names(self) -> None:
        for index, price in enumerate((45.0, 50.0, 55.0), start=1):
            self.add_sold(
                f"rst-{index}",
                search_id="rst_boots",
                search_name="RST Boots",
                brand="RST",
                size="UK 9",
                price=price,
            )

        listing = make_listing(
            "new-rst",
            search_id="motorcycle_boots",
            search_name="Motorcycle Boots",
            brand="RST",
            size="UK 9",
            price=20.0,
        )

        analysis = self.scorer.analyse(listing)

        assert analysis is not None
        self.assertEqual(analysis.estimated_resale, 50.0)
        self.assertEqual(analysis.comparable_count, 3)
        self.assertTrue(analysis.scope.startswith("brand+size"))

    def test_insufficient_brand_sample_falls_back_to_current_search(self) -> None:
        for index, price in enumerate((55.0, 60.0), start=1):
            self.add_sold(
                f"old-brand-{index}",
                search_id="old_search",
                search_name="Old Search",
                brand="RareBrand",
                size="UK 10",
                price=price,
            )

        for index, price in enumerate((28.0, 30.0, 32.0), start=1):
            self.add_sold(
                f"current-{index}",
                search_id="motorcycle_boots",
                search_name="Motorcycle Boots",
                brand="OtherBrand",
                size="UK 8",
                price=price,
            )

        listing = make_listing(
            "new-rare",
            search_id="motorcycle_boots",
            search_name="Motorcycle Boots",
            brand="RareBrand",
            size="UK 10",
            price=15.0,
        )

        analysis = self.scorer.analyse(listing)

        assert analysis is not None
        self.assertEqual(analysis.estimated_resale, 30.0)
        self.assertEqual(analysis.scope, "search:motorcycle_boots")

    def test_large_discount_produces_scored_opportunity(self) -> None:
        for index in range(12):
            self.add_sold(
                f"scarpa-{index}",
                brand="Scarpa",
                size="UK 8",
                condition="Very Good",
                price=40.0,
            )

        listing = make_listing(
            "new-scarpa",
            brand="Scarpa",
            size="UK 8",
            condition="Very Good",
            price=10.0,
        )

        analysis = self.scorer.analyse(listing)

        assert analysis is not None
        self.assertIsNotNone(analysis.score)
        self.assertGreaterEqual(analysis.score or 0, 65)
        self.assertEqual(analysis.estimated_resale, 40.0)
        self.assertEqual(analysis.gross_margin, 30.0)
        self.assertEqual(analysis.roi_percent, 300.0)

    def test_telegram_market_block_contains_estimate_and_score(self) -> None:
        analysis = OpportunityAnalysis(
            score=72,
            label="strong",
            confidence="medium",
            estimated_resale=45.0,
            gross_margin=25.0,
            roi_percent=125.0,
            comparable_count=14,
            sell_through_rate=74.0,
            average_days_to_sell=8.5,
            scope="brand:RST",
        )

        text = TelegramClient._opportunity_text(
            analysis,
            "£20.00",
        )

        self.assertIn("72/100", text)
        self.assertIn("Est. resale: £45.00", text)
        self.assertIn("Gross spread: £25.00", text)
        self.assertIn("14 sold comps", text)
        self.assertIn("Confidence: MEDIUM", text)


if __name__ == "__main__":
    unittest.main()
