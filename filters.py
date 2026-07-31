"""Filtering rules for Vinted listings."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

from models import Listing
from search import Search


def allow(listing: Listing, search: Search) -> bool:
    """Return whether a listing passes every filter configured for a search."""

    combined_text = _normalise_text(
        f"{listing.title} {listing.subtitle}"
    )
    subtitle = _normalise_text(listing.subtitle)

    if search.keywords and not _contains_any_phrase(
        combined_text,
        search.keywords,
    ):
        return False

    if (
        search.max_price is not None
        and listing.price_value > float(search.max_price)
    ):
        return False

    if search.sizes and not _contains_any_exact_value(
        subtitle,
        search.sizes,
    ):
        return False

    if search.conditions and not _contains_any_phrase(
        subtitle,
        search.conditions,
    ):
        return False

    return True


def _contains_any_phrase(
    text: str,
    values: Iterable[object],
) -> bool:
    """Return whether normalised text contains any non-empty phrase."""

    return any(
        phrase in text
        for value in values
        if (phrase := _normalise_text(value))
    )


def _contains_any_exact_value(
    text: str,
    values: Iterable[object],
) -> bool:
    """Match values using boundaries to avoid partial size matches.

    For example, size ``4`` will not match ``44`` and size ``L`` will not
    match a word such as ``excellent``.
    """

    return any(
        _value_pattern(_normalise_text(value)).search(text) is not None
        for value in values
        if _normalise_text(value)
    )


@lru_cache(maxsize=256)
def _value_pattern(value: str) -> re.Pattern[str]:
    escaped = re.escape(value).replace(r"\ ", r"\s+")

    return re.compile(
        rf"(?<![a-z0-9]){escaped}(?![a-z0-9])",
        flags=re.IGNORECASE,
    )


def _normalise_text(value: object | None) -> str:
    """Convert a value to consistent text for case-insensitive matching."""

    return " ".join(
        str(value or "")
        .casefold()
        .split()
    )