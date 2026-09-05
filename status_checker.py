"""Check whether stored Vinted listings are active, sold, or unavailable."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT: Final[tuple[int, int]] = (10, 30)

USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


class ListingStatus(StrEnum):
    """Known availability states for a stored Vinted listing."""

    ACTIVE = "active"
    SOLD = "sold"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Result of checking one Vinted listing URL."""

    status: ListingStatus
    http_status: int | None = None


class ListingStatusChecker:
    """Check Vinted item pages without opening additional Selenium tabs."""

    def __init__(self) -> None:
        self._session = self._create_session()

    def check(self, url: str) -> StatusResult:
        """Return the current availability state of a Vinted listing."""

        if not url or not url.strip():
            return StatusResult(ListingStatus.UNKNOWN)

        try:
            response = self._session.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,image/avif,"
                        "image/webp,*/*;q=0.8"
                    ),
                    "Accept-Language": "en-GB,en;q=0.9",
                },
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            LOGGER.warning(
                "Unable to check listing status for %s: %s",
                url,
                exc,
            )
            return StatusResult(ListingStatus.UNKNOWN)

        http_status = response.status_code

        if http_status in {404, 410}:
            LOGGER.debug("Listing not found: %s", url)

            return StatusResult(
                ListingStatus.NOT_FOUND,
                http_status=http_status,
            )

        if http_status != 200:
            LOGGER.warning(
                "Unexpected HTTP %s while checking %s",
                http_status,
                url,
            )

            return StatusResult(
                ListingStatus.UNKNOWN,
                http_status=http_status,
            )

        status = self._detect_status(response.text)

        LOGGER.debug(
            "Listing status: %s | %s",
            status.value,
            url,
        )

        return StatusResult(
            status,
            http_status=http_status,
        )

    def close(self) -> None:
        """Close the reusable HTTP session."""

        self._session.close()

    def __enter__(self) -> ListingStatusChecker:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.close()

    @staticmethod
    def _detect_status(html: str) -> ListingStatus:
        """Detect listing status from Vinted's public item page."""

        content = html.casefold()

        # Next.js data may escape URL slashes.
        content = content.replace("\\/", "/")

        sold_signals = (
            "schema.org/outofstock",
            '"is_sold":true',
            '\\"is_sold\\":true',
        )

        if any(signal in content for signal in sold_signals):
            return ListingStatus.SOLD

        active_signals = (
            "schema.org/instock",
            'data-testid="item-buy-button"',
            '"is_sold":false',
            '\\"is_sold\\":false',
        )

        if any(signal in content for signal in active_signals):
            return ListingStatus.ACTIVE

        return ListingStatus.UNKNOWN

    @staticmethod
    def _create_session() -> requests.Session:
        session = requests.Session()

        retry_policy = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=1.0,
            status_forcelist=(
                429,
                500,
                502,
                503,
                504,
            ),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )

        adapter = HTTPAdapter(
            max_retries=retry_policy,
            pool_connections=4,
            pool_maxsize=4,
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session