"""
Vinted Agent V3

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
    """Represents a single Vinted listing."""

    id: str

    title: str
    subtitle: str

    price: str
    total_price: str

    url: str
    image: str

    search_id: str | None = None
    search_name: str | None = None

    # Optional enriched item-page data.
    size: str | None = None
    condition: str | None = None
    brand: str | None = None
    description: str | None = None
    pictures: list[str] = field(default_factory=list)
    posted_at: str | None = None
    local_image_path: str | None = None

    price_value: float = field(init=False)
    total_price_value: float = field(init=False)

    def __post_init__(self) -> None:
        self.price_value = extract_price(self.price)
        self.total_price_value = extract_price(self.total_price)

        self.size = self._clean_optional(self.size)
        self.condition = self._clean_optional(self.condition)
        self.brand = self._clean_optional(self.brand)
        self.description = self._clean_optional(self.description)
        self.posted_at = self._clean_optional(self.posted_at)
        self.local_image_path = self._clean_optional(self.local_image_path)

        self.pictures = self._normalise_pictures(
            self.pictures,
            fallback=self.image,
        )

    @property
    def is_discounted(self) -> bool:
        """Return True when total price is higher than item price."""

        return self.total_price_value > self.price_value

    def to_dict(self) -> dict:
        """Serialize the listing."""

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
            "size": self.size,
            "condition": self.condition,
            "brand": self.brand,
            "description": self.description,
            "pictures": list(self.pictures),
            "posted_at": self.posted_at,
            "local_image_path": self.local_image_path,
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

    @staticmethod
    def _clean_optional(value: object | None) -> str | None:
        if value is None:
            return None

        text = str(value).strip()
        return text or None

    @staticmethod
    def _normalise_pictures(
        pictures: list[str] | None,
        *,
        fallback: str = "",
    ) -> list[str]:
        result: list[str] = []

        for picture in pictures or []:
            value = str(picture).strip()

            if value and value not in result:
                result.append(value)

        fallback_value = str(fallback or "").strip()

        if fallback_value and fallback_value not in result:
            result.insert(0, fallback_value)

        return result
