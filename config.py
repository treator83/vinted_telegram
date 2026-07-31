"""Environment-based configuration for Vinted Agent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

BASE_DIR: Final[Path] = Path(__file__).resolve().parent
ENV_FILE: Final[Path] = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_FILE)


class ConfigurationError(RuntimeError):
    """Raised when an environment variable contains an invalid value."""


def _get_string(name: str, default: str = "") -> str:
    """Return a stripped environment variable."""

    return os.getenv(name, default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    """Return a boolean environment variable."""

    raw_value = os.getenv(name)

    if raw_value is None or not raw_value.strip():
        return default

    value = raw_value.strip().casefold()

    true_values = {"1", "true", "yes", "on"}
    false_values = {"0", "false", "no", "off"}

    if value in true_values:
        return True

    if value in false_values:
        return False

    raise ConfigurationError(
        f"{name} must be one of: "
        f"{', '.join(sorted(true_values | false_values))}"
    )


def _get_int(
    name: str,
    default: int,
    minimum: int | None = None,
) -> int:
    """Return a validated integer environment variable."""

    raw_value = os.getenv(name)

    if raw_value is None or not raw_value.strip():
        value = default
    else:
        try:
            value = int(raw_value.strip())
        except ValueError as exc:
            raise ConfigurationError(
                f"{name} must be an integer"
            ) from exc

    if minimum is not None and value < minimum:
        raise ConfigurationError(
            f"{name} must be at least {minimum}"
        )

    return value


def _get_float(
    name: str,
    default: float,
    minimum: float | None = None,
) -> float:
    """Return a validated floating-point environment variable."""

    raw_value = os.getenv(name)

    if raw_value is None or not raw_value.strip():
        value = default
    else:
        try:
            value = float(raw_value.strip())
        except ValueError as exc:
            raise ConfigurationError(
                f"{name} must be a number"
            ) from exc

    if minimum is not None and value < minimum:
        raise ConfigurationError(
            f"{name} must be at least {minimum}"
        )

    return value


def _get_list(
    name: str,
    *,
    lowercase: bool = False,
) -> list[str]:
    """Return a comma-separated environment variable as a clean list."""

    values: list[str] = []

    for item in os.getenv(name, "").split(","):
        value = item.strip()

        if not value:
            continue

        if lowercase:
            value = value.casefold()

        values.append(value)

    return values


# Telegram
BOT_TOKEN: Final[str] = _get_string("BOT_TOKEN")
CHAT_ID: Final[str] = _get_string("CHAT_ID")


# Application
HEADLESS: Final[bool] = _get_bool("HEADLESS", default=False)
CHECK_INTERVAL: Final[int] = _get_int(
    "CHECK_INTERVAL",
    default=60,
    minimum=1,
)


# Browser overrides
CHROME_BINARY: Final[str | None] = (
    _get_string("CHROME_BINARY") or None
)
CHROMEDRIVER_PATH: Final[str | None] = (
    _get_string("CHROMEDRIVER_PATH") or None
)


# Project paths
DATA_DIR: Final[Path] = BASE_DIR / "data"
LOG_DIR: Final[Path] = BASE_DIR / "logs"
DATABASE_PATH: Final[Path] = DATA_DIR / "listings.db"
SEARCHES_FILE: Final[Path] = BASE_DIR / "searches.json"
LOG_FILE: Final[Path] = LOG_DIR / "vinted.log"


# Legacy settings retained temporarily for backward compatibility.
# Search-specific configuration should now be stored in searches.json.
SEARCH_URLS: Final[list[str]] = [
    value
    for index in range(1, 5)
    if (value := _get_string(f"SEARCH_URL_{index}"))
]

KEYWORDS: Final[list[str]] = _get_list(
    "KEYWORDS",
    lowercase=True,
)

MAX_PRICE: Final[float] = _get_float(
    "MAX_PRICE",
    default=999_999.0,
    minimum=0.0,
)

SIZES: Final[list[str]] = _get_list("SIZES")

CONDITIONS: Final[list[str]] = _get_list(
    "CONDITIONS",
    lowercase=True,
)