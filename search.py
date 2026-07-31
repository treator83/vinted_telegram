"""Search configuration model for Vinted Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(slots=True)
class Search:
    """Define one Vinted search and its listing filters."""

    id: str
    name: str
    url: str
    max_price: float | None = None
    keywords: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate and normalise values loaded from configuration."""

        self.id = str(self.id).strip()
        self.name = str(self.name).strip()
        self.url = str(self.url).strip()

        if not self.id:
            raise ValueError("Search id cannot be empty")

        if not self.name:
            raise ValueError("Search name cannot be empty")

        if not self.url:
            raise ValueError(f"Search URL cannot be empty: {self.name}")

        if self.max_price is not None:
            self.max_price = float(self.max_price)

            if self.max_price < 0:
                raise ValueError(
                    f"Search max_price cannot be negative: {self.name}"
                )

        self.keywords = self._normalise_values(self.keywords)
        self.sizes = self._normalise_values(self.sizes)
        self.conditions = self._normalise_values(self.conditions)

    @property
    def has_filters(self) -> bool:
        """Return whether this search has any configured filters."""

        return bool(
            self.keywords
            or self.sizes
            or self.conditions
            or self.max_price is not None
        )

    def __str__(self) -> str:
        if self.max_price is None:
            return self.name

        return f"{self.name} (max £{self.max_price:.2f})"

    @staticmethod
    def _normalise_values(values: Iterable[object] | None) -> list[str]:
        """Return unique, non-empty configuration values."""

        if values is None:
            return []

        normalised: list[str] = []
        seen: set[str] = set()

        for value in values:
            text = str(value).strip()

            if not text:
                continue

            comparison_value = text.casefold()

            if comparison_value in seen:
                continue

            seen.add(comparison_value)
            normalised.append(text)

        return normalised