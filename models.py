"""
Vinted Agent V2

models.py

Domain models used throughout the application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_PRICE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")


def extract_price(text: str | None) -> float:
    """
    Convert a price string to a float.

    Examples:
        £74.20 -> 74.20
        €99,95 -> 99.95

    Returns:
        float
    """

    if not text:
        return 0.0

    text = text.replace(",", ".")

    match = _PRICE_PATTERN.search(text)

    if not match:
        return 0.0

    return float(match.group(1))


@dataclass(slots=True)
class Listing:
    """
    Represents a single Vinted listing.
    """

    id: str

    title: str
    subtitle: str

    price: str
    total_price: str

    url: str
    image: str

    search_id: str | None = None
    search_name: str | None = None

    price_value: float = field(init=False)
    total_price_value: float = field(init=False)

    def __post_init__(self) -> None:

        self.price_value = extract_price(self.price)
        self.total_price_value = extract_price(self.total_price)

    @property
    def is_discounted(self) -> bool:
        """
        Returns True if shipping appears to increase the total price.
        """

        return self.total_price_value > self.price_value

    def to_dict(self) -> dict:
        """
        Serialize the listing.
        """

        return {
            "id": self.id,
            "title": self.title,
            "subtitle": self.subtitle,
            "price": self.price,
            "total_price": self.total_price,
            "price_value": self.price_value,
            "total_price_value": self.total_price_value,
            "url": self.url,
            "image": self.image,
            "search_id": self.search_id,
            "search_name": self.search_name,
        }

    def __str__(self) -> str:

        return (
            f"{self.title} | "
            f"{self.price} | "
            f"{self.subtitle}"
        )

    def __repr__(self) -> str:

        return (
            f"Listing("
            f"id={self.id!r}, "
            f"title={self.title!r}, "
            f"price={self.price!r})"
        )