"""Filtering rules for Vinted listings."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

from models import Listing
from search import Search


_UK_SIZE_MIN = 0.0
_UK_SIZE_MAX = 15.0
_EU_SIZE_MIN = 30.0
_EU_SIZE_MAX = 52.0

# Approximate adult footwear conversion points. Brand charts can differ by
# half a size, but these are sufficient for matching Vinted listing labels.
_UK_TO_EU: dict[float, float] = {
    3.0: 36.0,
    3.5: 36.5,
    4.0: 37.0,
    4.5: 37.5,
    5.0: 38.0,
    5.5: 39.0,
    6.0: 39.5,
    6.5: 40.0,
    7.0: 41.0,
    7.5: 41.5,
    8.0: 42.0,
    8.5: 42.5,
    9.0: 43.0,
    9.5: 44.0,
    10.0: 44.5,
    10.5: 45.0,
    11.0: 46.0,
    11.5: 46.5,
    12.0: 47.0,
    12.5: 47.5,
    13.0: 48.0,
}

_SIZE_THRESHOLD_PATTERN = re.compile(
    r"^(?:uk\s*)?(\d{1,2}(?:[.,]\d)?)\s*\+$",
    flags=re.IGNORECASE,
)

_UK_SIZE_PATTERNS = (
    re.compile(
        r"\buk\s*(?:size\s*)?[:#-]?\s*(\d{1,2}(?:[.,]\d)?)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d{1,2}(?:[.,]\d)?)\s*uk\b",
        flags=re.IGNORECASE,
    ),
)

_EU_SIZE_PATTERNS = (
    re.compile(
        r"\beu\s*(?:size\s*)?[:#-]?\s*(\d{2}(?:[.,]\d)?)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d{2}(?:[.,]\d)?)\s*eu\b",
        flags=re.IGNORECASE,
    ),
)

_GENERIC_SIZE_PATTERN = re.compile(
    r"\bsize\s*[:#-]?\s*(\d{1,2}(?:[.,]\d)?)\b",
    flags=re.IGNORECASE,
)


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

    if search.sizes:
        size_text = _normalise_text(
            f"{listing.title} {listing.subtitle} "
            f"size {getattr(listing, 'size', None) or ''}"
        )

        if not _matches_size_filter(
            size_text,
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


def _matches_size_filter(
    text: str,
    values: Iterable[object],
) -> bool:
    """Match UK shoe sizes, including EU equivalents and ``4+`` thresholds.

    Numeric configured values are treated as UK sizes. Listing labels such as
    ``UK 7``, ``7 UK``, ``EU 41`` and ``Size 41`` are normalised to UK sizes.
    A configured value such as ``4+`` accepts UK size 4 and larger.
    Non-numeric size values still use exact boundary matching.
    """

    detected_uk_sizes = _extract_uk_sizes(text)

    for value in values:
        normalised = _normalise_text(value)

        if not normalised:
            continue

        threshold_match = _SIZE_THRESHOLD_PATTERN.fullmatch(
            normalised
        )

        if threshold_match is not None:
            minimum = _parse_number(
                threshold_match.group(1)
            )

            if minimum is None:
                continue

            if any(
                detected >= minimum
                for detected in detected_uk_sizes
            ):
                return True

            continue

        configured_size = _parse_number(normalised)

        if (
            configured_size is not None
            and _UK_SIZE_MIN <= configured_size <= _UK_SIZE_MAX
        ):
            if any(
                abs(detected - configured_size) < 0.01
                for detected in detected_uk_sizes
            ):
                return True

            # Preserve the old exact-number behaviour for catalogue text that
            # does not explicitly label the size.
            if _value_pattern(normalised).search(text) is not None:
                return True

            continue

        if _value_pattern(normalised).search(text) is not None:
            return True

    return False


def _extract_uk_sizes(text: str) -> list[float]:
    """Extract plausible footwear sizes and return them on the UK scale."""

    sizes: list[float] = []

    for pattern in _UK_SIZE_PATTERNS:
        for match in pattern.finditer(text):
            value = _parse_number(match.group(1))

            if (
                value is not None
                and _UK_SIZE_MIN <= value <= _UK_SIZE_MAX
            ):
                _append_unique_size(sizes, value)

    for pattern in _EU_SIZE_PATTERNS:
        for match in pattern.finditer(text):
            value = _parse_number(match.group(1))

            if (
                value is not None
                and _EU_SIZE_MIN <= value <= _EU_SIZE_MAX
            ):
                converted = _eu_to_uk(value)

                if converted is not None:
                    _append_unique_size(
                        sizes,
                        converted,
                    )

    for match in _GENERIC_SIZE_PATTERN.finditer(text):
        value = _parse_number(match.group(1))

        if value is None:
            continue

        if _UK_SIZE_MIN <= value <= _UK_SIZE_MAX:
            _append_unique_size(
                sizes,
                value,
            )
            continue

        if _EU_SIZE_MIN <= value <= _EU_SIZE_MAX:
            converted = _eu_to_uk(value)

            if converted is not None:
                _append_unique_size(
                    sizes,
                    converted,
                )

    return sizes


def _eu_to_uk(eu_size: float) -> float | None:
    """Return the nearest configured UK equivalent for an EU shoe size."""

    if not _UK_TO_EU:
        return None

    uk_size, mapped_eu = min(
        _UK_TO_EU.items(),
        key=lambda item: abs(item[1] - eu_size),
    )

    # Reject implausible values instead of silently converting arbitrary
    # numbers that happened to appear after a size label.
    if abs(mapped_eu - eu_size) > 0.75:
        return None

    return uk_size


def _append_unique_size(
    sizes: list[float],
    value: float,
) -> None:
    if not any(
        abs(existing - value) < 0.01
        for existing in sizes
    ):
        sizes.append(value)


def _parse_number(value: object | None) -> float | None:
    text = str(value or "").strip().replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return None


def _contains_any_exact_value(
    text: str,
    values: Iterable[object],
) -> bool:
    """Compatibility helper for exact boundary matching."""

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
