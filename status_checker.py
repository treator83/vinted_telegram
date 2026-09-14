"""Inspect Vinted item pages with the reusable Selenium browser."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Final

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from browser import Browser, BrowserError


LOGGER = logging.getLogger(__name__)

ITEM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"/items/(\d+)"
)

IMAGE_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r'"url":"(https://images1\.vinted\.net/[^"]+)"'
)


class ListingStatus(StrEnum):
    """Known availability states for a Vinted listing."""

    ACTIVE = "active"
    SOLD = "sold"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ListingDetails:
    """Enriched information collected from one Vinted item page."""

    size: str | None = None
    condition: str | None = None
    brand: str | None = None
    description: str | None = None
    pictures: tuple[str, ...] = ()
    posted_at: str | None = None
    upload_text: str | None = None


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Result of inspecting one Vinted listing."""

    status: ListingStatus
    reason: str
    http_status: int | None = None
    details: ListingDetails = field(
        default_factory=ListingDetails
    )


class ListingStatusChecker:
    """
    Inspect Vinted listing pages using Selenium.

    A Browser may be supplied so the checker shares the same Chromium
    session as the catalogue scraper.  This avoids the HTTP blocking and
    ambiguous responses seen with raw requests.
    """

    def __init__(
        self,
        browser: Browser | None = None,
    ) -> None:
        self.browser = browser or Browser()
        self._owns_browser = browser is None

    def check(
        self,
        url: str,
    ) -> StatusResult:
        """Open one item page and return status plus enriched details."""

        if not url or not url.strip():
            return StatusResult(
                status=ListingStatus.UNKNOWN,
                reason="missing_url",
            )

        try:
            self.browser.get(
                url
            )

            driver = self.browser.driver

            page_source = (
                driver.page_source
                or ""
            )

            current_url = (
                driver.current_url
                or url
            )

            visible_text = self._visible_text()

        except (
            BrowserError,
            WebDriverException,
        ) as exc:
            LOGGER.warning(
                "Unable to inspect listing page %s: %s",
                url,
                exc,
            )

            return StatusResult(
                status=ListingStatus.UNKNOWN,
                reason="browser_error",
            )

        status, reason = self._detect_status(
            page_source,
            current_url=current_url,
            visible_text=visible_text,
        )

        details = self._extract_details(
            page_source,
            listing_id=self._listing_id(
                url
            ),
        )

        # Prefer values visible in the rendered DOM when available.
        details = self._merge_dom_details(
            details
        )

        LOGGER.debug(
            "Listing inspection | status=%s | reason=%s | %s",
            status.value,
            reason,
            url,
        )

        return StatusResult(
            status=status,
            reason=reason,
            details=details,
        )

    def close(self) -> None:
        """Close an internally-owned browser, but not a shared browser."""

        if self._owns_browser:
            self.browser.stop()

    def __enter__(
        self,
    ) -> "ListingStatusChecker":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.close()

    def _visible_text(self) -> str:
        try:
            body = self.browser.driver.find_element(
                By.TAG_NAME,
                "body",
            )

            return (
                body.text
                or ""
            ).strip()

        except WebDriverException:
            return ""

    def _merge_dom_details(
        self,
        details: ListingDetails,
    ) -> ListingDetails:
        size = (
            self._dom_text(
                '[itemprop="size"]'
            )
            or details.size
        )

        condition = (
            self._dom_text(
                '[itemprop="status"]'
            )
            or details.condition
        )

        # Prefer the structured Next.js value because the rendered DOM can
        # append UI text such as "... more" to a collapsed description.
        description = (
            details.description
            or self._dom_text(
                '[itemprop="description"]'
            )
        )

        upload_text = (
            self._dom_text(
                '[itemprop="upload_date"]'
            )
            or details.upload_text
        )

        brand = (
            self._attribute_container_value(
                "brand"
            )
            or details.brand
        )

        posted_at = (
            self._upload_text_to_timestamp(
                upload_text
            )
            or details.posted_at
        )

        return ListingDetails(
            size=self._clean(size),
            condition=self._clean(condition),
            brand=self._clean(brand),
            description=self._clean(
                description
            ),
            pictures=details.pictures,
            posted_at=posted_at,
            upload_text=self._clean(
                upload_text
            ),
        )

    def _dom_text(
        self,
        selector: str,
    ) -> str | None:
        try:
            elements = (
                self.browser.driver
                .find_elements(
                    By.CSS_SELECTOR,
                    selector,
                )
            )

            for element in elements:
                text = (
                    element.text
                    or ""
                ).strip()

                if text:
                    return text

        except WebDriverException:
            return None

        return None

    def _attribute_container_value(
        self,
        code: str,
    ) -> str | None:
        selector = (
            f'[data-testid="item-attributes-{code}"]'
        )

        try:
            elements = (
                self.browser.driver
                .find_elements(
                    By.CSS_SELECTOR,
                    selector,
                )
            )

            for element in elements:
                lines = [
                    line.strip()
                    for line in (
                        element.text
                        or ""
                    ).splitlines()
                    if line.strip()
                ]

                if len(lines) >= 2:
                    return lines[-1]

        except WebDriverException:
            return None

        return None

    @classmethod
    def _detect_status(
        cls,
        html: str,
        *,
        current_url: str = "",
        visible_text: str = "",
    ) -> tuple[ListingStatus, str]:
        """
        Detect listing availability.

        NOT_FOUND remains a separate audit reason, while the database/report
        layer counts it as sold per project rules.
        """

        content = cls._normalise_html(
            html
        ).casefold()

        visible = (
            visible_text
            or ""
        ).casefold()

        current = (
            current_url
            or ""
        ).casefold()

        sold_signals = (
            (
                "schema.org/outofstock",
                "schema_out_of_stock",
            ),
            (
                '"is_sold":true',
                "is_sold",
            ),
            (
                '"can_buy":false',
                "cannot_buy",
            ),
        )

        for signal, reason in sold_signals:
            if signal in content:
                return (
                    ListingStatus.SOLD,
                    reason,
                )

        not_found_url_signals = (
            "/404",
            "/not-found",
            "/not_found",
        )

        if any(
            signal in current
            for signal in not_found_url_signals
        ):
            return (
                ListingStatus.NOT_FOUND,
                "not_found_redirect",
            )

        not_found_text_signals = (
            "item not found",
            "this item is no longer available",
            "this item doesn't exist",
            "this item does not exist",
            "page not found",
            "content not found",
        )

        if any(
            signal in visible
            for signal in not_found_text_signals
        ):
            return (
                ListingStatus.NOT_FOUND,
                "not_found_page",
            )

        active_signals = (
            (
                'data-testid="item-buy-button"',
                "buy_button",
            ),
            (
                "schema.org/instock",
                "schema_in_stock",
            ),
            (
                '"is_sold":false',
                "is_sold_false",
            ),
            (
                '"can_buy":true',
                "can_buy",
            ),
        )

        for signal, reason in active_signals:
            if signal in content:
                return (
                    ListingStatus.ACTIVE,
                    reason,
                )

        return (
            ListingStatus.UNKNOWN,
            "no_status_signal",
        )

    @classmethod
    def _extract_details(
        cls,
        html: str,
        *,
        listing_id: str | None = None,
    ) -> ListingDetails:
        """Extract item details from Vinted's rendered HTML/Next.js data."""

        content = cls._normalise_html(
            html
        )

        size = cls._attribute_value(
            content,
            "size",
        )

        condition = cls._attribute_value(
            content,
            "status",
        )

        brand = cls._attribute_value(
            content,
            "brand",
        )

        upload_text = cls._attribute_value(
            content,
            "upload_date",
        )

        description = cls._json_string_value(
            content,
            "description",
            following_key="is_expanded",
        )

        pictures = cls._extract_photo_urls(
            content,
            listing_id=listing_id,
        )

        posted_at = (
            cls._upload_text_to_timestamp(
                upload_text
            )
        )

        return ListingDetails(
            size=cls._clean(size),
            condition=cls._clean(
                condition
            ),
            brand=cls._clean(brand),
            description=cls._clean(
                description
            ),
            pictures=tuple(
                pictures
            ),
            posted_at=posted_at,
            upload_text=cls._clean(
                upload_text
            ),
        )

    @staticmethod
    def _attribute_value(
        content: str,
        code: str,
    ) -> str | None:
        pattern = re.compile(
            rf'"code":"{re.escape(code)}",'
            rf'"data":\{{'
            rf'.{{0,1000}}?'
            rf'"value":"((?:\\.|[^"])*)"',
            re.DOTALL,
        )

        match = pattern.search(
            content
        )

        if match is None:
            return None

        return ListingStatusChecker._decode_text(
            match.group(1)
        )

    @staticmethod
    def _json_string_value(
        content: str,
        key: str,
        *,
        following_key: str | None = None,
    ) -> str | None:
        if following_key:
            suffix = (
                rf'","{re.escape(following_key)}"'
            )
        else:
            suffix = '"'

        pattern = re.compile(
            rf'"{re.escape(key)}":'
            rf'"((?:\\.|[^"])*)'
            rf'{suffix}',
            re.DOTALL,
        )

        match = pattern.search(
            content
        )

        if match is None:
            return None

        return ListingStatusChecker._decode_text(
            match.group(1)
        )

    @staticmethod
    def _extract_photo_urls(
        content: str,
        *,
        listing_id: str | None,
    ) -> list[str]:
        start = -1

        if listing_id:
            marker = (
                f'"item_id":"{listing_id}",'
                '"photos":['
            )

            start = content.find(
                marker
            )

            if start >= 0:
                start += len(
                    marker
                )

        if start < 0:
            marker = '"photos":['

            start = content.find(
                marker
            )

            if start < 0:
                return []

            start += len(
                marker
            )

        end_candidates: list[int] = []

        for delimiter in (
            '],"price":',
            '],"seller_id":',
            '],"title":',
        ):
            index = content.find(
                delimiter,
                start,
            )

            if index >= 0:
                end_candidates.append(
                    index
                )

        if end_candidates:
            end = min(
                end_candidates
            )
        else:
            end = min(
                len(content),
                start + 200_000,
            )

        segment = content[
            start:end
        ]

        result: list[str] = []

        for match in IMAGE_URL_PATTERN.finditer(
            segment
        ):
            url = (
                match.group(1)
                .replace(
                    "\\u0026",
                    "&",
                )
                .replace(
                    "\\/",
                    "/",
                )
            )

            # Vinted's full-size gallery images use /f800/.
            if "/f800/" not in url:
                continue

            if url not in result:
                result.append(
                    url
                )

        return result

    @staticmethod
    def _normalise_html(
        value: str,
    ) -> str:
        return (
            html_lib.unescape(
                value or ""
            )
            .replace(
                '\\"',
                '"',
            )
            .replace(
                "\\/",
                "/",
            )
        )

    @staticmethod
    def _decode_text(
        value: str,
    ) -> str:
        text = (
            value
            or ""
        )

        try:
            decoded = json.loads(
                f'"{text}"'
            )

            if isinstance(
                decoded,
                str,
            ):
                text = decoded

        except (
            json.JSONDecodeError,
            TypeError,
        ):
            pass

        # Next.js payloads are sometimes escaped twice.
        text = (
            text
            .replace(
                "\\n",
                "\n",
            )
            .replace(
                "\\r",
                "\r",
            )
            .replace(
                "\\t",
                "\t",
            )
            .replace(
                "\\u0026",
                "&",
            )
        )

        unicode_escape = re.compile(
            r"\\u([0-9a-fA-F]{4})"
        )

        text = unicode_escape.sub(
            lambda match: chr(
                int(
                    match.group(1),
                    16,
                )
            ),
            text,
        )

        return text.strip()

    @staticmethod
    def _upload_text_to_timestamp(
        value: str | None,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """
        Convert Vinted's relative upload label into an approximate UTC time.

        Vinted commonly renders labels such as "21 min ago" rather than an
        exact timestamp, so the resulting value is intentionally approximate.
        """

        if not value:
            return None

        text = (
            str(value)
            .strip()
            .casefold()
        )

        if text.startswith(
            "uploaded "
        ):
            text = text[
                len("uploaded "):
            ].strip()

        current = (
            now
            or datetime.now(
                timezone.utc
            )
        )

        if current.tzinfo is None:
            current = current.replace(
                tzinfo=timezone.utc
            )

        patterns = (
            (
                r"^(\d+)\s*(?:sec|secs|second|seconds)\s+ago$",
                "seconds",
            ),
            (
                r"^(\d+)\s*(?:min|mins|minute|minutes)\s+ago$",
                "minutes",
            ),
            (
                r"^(\d+)\s*(?:hr|hrs|hour|hours)\s+ago$",
                "hours",
            ),
            (
                r"^(\d+)\s*(?:day|days)\s+ago$",
                "days",
            ),
            (
                r"^(\d+)\s*(?:week|weeks)\s+ago$",
                "weeks",
            ),
        )

        for pattern, unit in patterns:
            match = re.match(
                pattern,
                text,
            )

            if match is None:
                continue

            amount = int(
                match.group(1)
            )

            delta = timedelta(
                **{
                    unit: amount,
                }
            )

            estimated = (
                current
                .astimezone(
                    timezone.utc
                )
                - delta
            )

            return estimated.strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        singular_relative = {
            "a few seconds ago": timedelta(
                seconds=5
            ),
            "a minute ago": timedelta(
                minutes=1
            ),
            "an hour ago": timedelta(
                hours=1
            ),
            "a day ago": timedelta(
                days=1
            ),
            "a week ago": timedelta(
                weeks=1
            ),
            "yesterday": timedelta(
                days=1
            ),
        }

        if text in singular_relative:
            estimated = (
                current
                .astimezone(
                    timezone.utc
                )
                - singular_relative[
                    text
                ]
            )

            return estimated.strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        # Preserve exact ISO-style timestamps if Vinted supplies one later.
        try:
            parsed = datetime.fromisoformat(
                text.replace(
                    "z",
                    "+00:00",
                )
            )

        except ValueError:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return (
            parsed
            .astimezone(
                timezone.utc
            )
            .strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

    @staticmethod
    def _listing_id(
        url: str,
    ) -> str | None:
        match = ITEM_ID_PATTERN.search(
            url or ""
        )

        if match is None:
            return None

        return match.group(1)

    @staticmethod
    def _clean(
        value: str | None,
    ) -> str | None:
        if value is None:
            return None

        text = str(
            value
        ).strip()

        return text or None
