"""Tests for smart footwear size filtering."""

from __future__ import annotations

import unittest

from filters import allow
from models import Listing
from search import Search


def make_listing(
    subtitle: str,
    *,
    title: str = "Climbing Shoes",
) -> Listing:
    return Listing(
        id="size-test",
        title=title,
        subtitle=subtitle,
        price="£15.00",
        total_price="£17.00",
        url="https://www.vinted.co.uk/items/size-test",
        image="",
        search_id="climbing_shoes",
        search_name="Climbing Shoes",
    )


def make_search(
    sizes: list[str],
) -> Search:
    return Search(
        id="climbing_shoes",
        name="Climbing Shoes",
        url="https://www.vinted.co.uk/catalog",
        max_price=20,
        keywords=[],
        sizes=sizes,
        conditions=[],
    )


class SmartSizeFilterTests(unittest.TestCase):
    def test_matches_uk_prefix(self) -> None:
        self.assertTrue(
            allow(
                make_listing("UK 7 · Good"),
                make_search(["7"]),
            )
        )

    def test_matches_uk_suffix(self) -> None:
        self.assertTrue(
            allow(
                make_listing("7 UK · Good"),
                make_search(["7"]),
            )
        )

    def test_matches_eu_equivalent(self) -> None:
        self.assertTrue(
            allow(
                make_listing("EU 41 · Good"),
                make_search(["7"]),
            )
        )

    def test_matches_unqualified_eu_size(self) -> None:
        self.assertTrue(
            allow(
                make_listing("Size 41 · Good"),
                make_search(["7"]),
            )
        )

    def test_four_plus_accepts_uk_four(self) -> None:
        self.assertTrue(
            allow(
                make_listing("UK 4 · Good"),
                make_search(["4+"]),
            )
        )

    def test_four_plus_accepts_larger_half_size(self) -> None:
        self.assertTrue(
            allow(
                make_listing("UK 8.5 · Good"),
                make_search(["4+"]),
            )
        )

    def test_four_plus_accepts_eu_37_and_above(self) -> None:
        self.assertTrue(
            allow(
                make_listing("EU 37 · Good"),
                make_search(["4+"]),
            )
        )
        self.assertTrue(
            allow(
                make_listing("Size 44 · Good"),
                make_search(["4+"]),
            )
        )

    def test_four_plus_rejects_sizes_below_uk_four(self) -> None:
        self.assertFalse(
            allow(
                make_listing("UK 3.5 · Good"),
                make_search(["4+"]),
            )
        )
        self.assertFalse(
            allow(
                make_listing("EU 36 · Good"),
                make_search(["4+"]),
            )
        )

    def test_four_plus_rejects_implausible_unqualified_size(self) -> None:
        self.assertFalse(
            allow(
                make_listing("Size 19 · Good"),
                make_search(["4+"]),
            )
        )

    def test_exact_size_still_avoids_partial_number_match(self) -> None:
        self.assertFalse(
            allow(
                make_listing("Size 19 · Good"),
                make_search(["9"]),
            )
        )


if __name__ == "__main__":
    unittest.main()
