"""Check whether stored Vinted listings are active or sold."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT: Final[tuple[int, int]] = (
    10,
    30,
)

USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


class ListingStatus(StrEnum):
    """Known availability states for a Vinted listing."""

    ACTIVE = "active"
    SOLD = "sold"

    # Kept for compatibility with existing code/database.
    # New HTTP 404/410 responses are now treated as SOLD.
    NOT_FOUND = "not_found"

    UNKNOWN = "unknown"


@dataclass(
    frozen=True,
    slots=True,
)
class StatusResult:
    """Result of checking one Vinted listing."""

    status: ListingStatus
    http_status: int | None = None


class ListingStatusChecker:
    """Check Vinted listing availability using HTTP."""

    def __init__(self) -> None:
        self._session = self._create_session()

    def check(
        self,
        url: str,
    ) -> StatusResult:
        """
        Check the current state of one listing.

        Important project rule:

        HTTP 404 and HTTP 410 are treated as SOLD.
        """

        if not url or not url.strip():
            return StatusResult(
                ListingStatus.UNKNOWN
            )

        try:
            response = self._session.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": (
                        "text/html,"
                        "application/xhtml+xml,"
                        "application/xml;q=0.9,"
                        "image/avif,"
                        "image/webp,"
                        "*/*;q=0.8"
                    ),
                    "Accept-Language": (
                        "en-GB,en;q=0.9"
                    ),
                },
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

        except requests.RequestException as exc:
            LOGGER.warning(
                "Unable to check listing status "
                "for %s: %s",
                url,
                exc,
            )

            return StatusResult(
                ListingStatus.UNKNOWN
            )

        http_status = response.status_code

        # Project rule:
        # unavailable Vinted item pages count as sold.
        if http_status in {
            404,
            410,
        }:
            LOGGER.info(
                "Listing unavailable (HTTP %s); "
                "treating as SOLD | %s",
                http_status,
                url,
            )

            return StatusResult(
                ListingStatus.SOLD,
                http_status=http_status,
            )

        if http_status != 200:
            LOGGER.warning(
                "Unexpected HTTP %s while "
                "checking %s",
                http_status,
                url,
            )

            return StatusResult(
                ListingStatus.UNKNOWN,
                http_status=http_status,
            )

        status = self._detect_status(
            response.text
        )

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

    def __enter__(
        self,
    ) -> ListingStatusChecker:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.close()

    @staticmethod
    def _detect_status(
        html: str,
    ) -> ListingStatus:
        """Detect availability from Vinted HTML."""

        content = html.casefold()

        # Vinted/Next.js may escape slashes.
        content = content.replace(
            "\\/",
            "/",
        )

        sold_signals = (
            "schema.org/outofstock",
            '"is_sold":true',
            '\\"is_sold\\":true',
        )

        if any(
            signal in content
            for signal in sold_signals
        ):
            return ListingStatus.SOLD

        active_signals = (
            "schema.org/instock",
            'data-testid="item-buy-button"',
            '"is_sold":false',
            '\\"is_sold\\":false',
        )

        if any(
            signal in content
            for signal in active_signals
        ):
            return ListingStatus.ACTIVE

        return ListingStatus.UNKNOWN

    @staticmethod
    def _create_session() -> requests.Session:
        """Create reusable requests session."""

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
            allowed_methods=frozenset(
                {
                    "GET",
                }
            ),
            respect_retry_after_header=True,
        )

        adapter = HTTPAdapter(
            max_retries=retry_policy,
            pool_connections=4,
            pool_maxsize=4,
        )

        session.mount(
            "https://",
            adapter,
        )

        session.mount(
            "http://",
            adapter,
        )

        return session