"""Main entry point for Vinted Agent."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from dataclasses import dataclass

from config import CHECK_INTERVAL, DATABASE_PATH
from database import Database
from filters import allow
from logger import configure_logging
from models import Listing
from scraper import VintedScraper
from search import Search
from search_manager import SearchManager
from status_checker import (
    ListingDetails,
    ListingStatus,
    ListingStatusChecker,
    StatusResult,
)
from telegram_client import TelegramClient


LOGGER = logging.getLogger(__name__)

STOP_EVENT = threading.Event()

PRICE_TOLERANCE = 0.005
STATUS_CHECK_BATCH_SIZE = 10
STATUS_CHECK_DELAY = 1.0
NOTIFICATION_BATCH_SIZE = 20


@dataclass(slots=True)
class CycleStats:
    """Statistics collected during one monitoring cycle."""

    listings_found: int = 0
    listings_filtered: int = 0

    new_listings: int = 0

    price_drops: int = 0
    price_increases: int = 0

    failed_listings: int = 0
    failed_searches: int = 0

    status_checks: int = 0
    sold_detected: int = 0
    unavailable_detected: int = 0
    status_failures: int = 0
    enriched_listings: int = 0

    notifications_sent: int = 0
    notification_failures: int = 0


class VintedAgent:
    """Long-running Vinted monitoring service."""

    def __init__(self) -> None:
        self.database = Database(DATABASE_PATH)
        self.scraper = VintedScraper()
        self.telegram = TelegramClient()
        self.search_manager = SearchManager()
        self.status_checker = ListingStatusChecker(
            browser=self.scraper.browser
        )

        self.started = False

    def start(self) -> None:
        """Start reusable application resources."""

        if self.started:
            return

        LOGGER.info("=" * 60)
        LOGGER.info("Starting Vinted Agent")
        LOGGER.info("=" * 60)

        self.scraper.start()

        self.started = True

    def run_forever(self) -> None:
        """Run monitoring cycles until shutdown."""

        self.start()

        while not STOP_EVENT.is_set():
            cycle_started = time.monotonic()

            try:
                self.run_cycle()

            except Exception:
                LOGGER.exception(
                    "Unexpected monitoring cycle failure"
                )

            elapsed = time.monotonic() - cycle_started

            LOGGER.info(
                "Cycle completed in %.2f seconds; "
                "sleeping for %s seconds",
                elapsed,
                CHECK_INTERVAL,
            )

            if STOP_EVENT.wait(CHECK_INTERVAL):
                break

    def run_cycle(self) -> CycleStats:
        """Run one complete monitoring cycle."""

        stats = CycleStats()

        # Retry any durable Telegram notifications left from a
        # previous cycle before doing new scraping work.
        self._process_notification_queue(
            stats
        )

        try:
            searches = self.search_manager.load()

        except Exception:
            LOGGER.exception(
                "Unable to load search configuration"
            )

            stats.failed_searches += 1
            return stats

        LOGGER.info("=" * 60)
        LOGGER.info(
            "Starting monitoring cycle with %d searches",
            len(searches),
        )
        LOGGER.info("=" * 60)

        for search in searches:
            if STOP_EVENT.is_set():
                break

            try:
                self._process_search(
                    search,
                    stats,
                )

            except Exception:
                stats.failed_searches += 1

                LOGGER.exception(
                    "Search failed: %s",
                    search.name,
                )

            LOGGER.info("-" * 60)

        if not STOP_EVENT.is_set():
            self._check_listing_statuses(
                stats
            )

        if not STOP_EVENT.is_set():
            # Deliver notifications queued during this cycle.
            self._process_notification_queue(
                stats
            )

        self._log_cycle_summary(
            stats
        )

        return stats

    def _process_search(
        self,
        search: Search,
        stats: CycleStats,
    ) -> None:
        """Scrape and process one configured search."""

        LOGGER.info(
            "Searching: %s",
            search.name,
        )

        # scraper.open() receives the URL.
        self.scraper.open(
            search.url
        )

        # scraper.fetch() parses the page already opened.
        listings = self.scraper.fetch()

        stats.listings_found += len(
            listings
        )

        LOGGER.info(
            "Found %d listings",
            len(listings),
        )

        new_for_search = 0

        for listing in listings:
            if STOP_EVENT.is_set():
                break

            try:
                listing.search_id = search.id
                listing.search_name = search.name

                if not allow(
                    listing,
                    search,
                ):
                    stats.listings_filtered += 1
                    continue

                is_new = self._process_listing(
                    listing,
                    stats,
                    search,
                )

                if is_new:
                    new_for_search += 1
                    stats.new_listings += 1

            except Exception:
                stats.failed_listings += 1

                LOGGER.exception(
                    "Unable to process listing %s",
                    getattr(
                        listing,
                        "id",
                        "unknown",
                    ),
                )

        LOGGER.info(
            "New listings: %d",
            new_for_search,
        )

    def _process_listing(
        self,
        listing: Listing,
        stats: CycleStats,
        search: Search,
    ) -> bool:
        """
        Process one allowed listing.

        Returns True only for newly discovered listings.
        """

        stored = self.database.get(
            listing.id
        )

        if stored is None:
            inspection = self._inspect_new_listing(
                listing,
                stats,
            )

            inserted = self.database.save(
                listing,
                search_config=self._search_config(
                    search
                ),
            )

            if not inserted:
                return False

            if inspection is not None:
                self.database.set_listing_status(
                    listing.id,
                    inspection.status.value,
                    reason=inspection.reason,
                )

            if (
                inspection is not None
                and inspection.status
                in {
                    ListingStatus.SOLD,
                    ListingStatus.NOT_FOUND,
                }
            ):
                LOGGER.info(
                    "NEW BUT UNAVAILABLE | %s | %s | %s",
                    listing.search_name,
                    listing.title,
                    inspection.reason,
                )
                return True

            self.database.enqueue_notification(
                listing.id,
                "new_listing",
                payload=listing.to_dict(),
            )

            LOGGER.info(
                "QUEUED NEW | %s | %s | %s",
                listing.search_name,
                listing.title,
                listing.price,
            )

            return True

        self.database.touch(
            listing.id
        )

        old_price = self._stored_price(
            stored
        )

        new_price = listing.price_value

        difference = (
            new_price - old_price
        )

        if abs(difference) < PRICE_TOLERANCE:
            return False

        updated = self.database.update_price(
            listing
        )

        if not updated:
            return False

        if difference < 0:
            stats.price_drops += 1

            self.database.enqueue_notification(
                listing.id,
                "price_drop",
                old_price=old_price,
                payload=listing.to_dict(),
            )

            LOGGER.info(
                "QUEUED PRICE DROP | %s | %s | "
                "£%.2f -> £%.2f",
                listing.search_name,
                listing.title,
                old_price,
                new_price,
            )

        else:
            stats.price_increases += 1

            LOGGER.info(
                "PRICE INCREASE | %s | %s | "
                "£%.2f -> £%.2f",
                listing.search_name,
                listing.title,
                old_price,
                new_price,
            )

        return False

    def _check_listing_statuses(
        self,
        stats: CycleStats,
    ) -> None:
        """
        Check stored item pages for availability.

        Listings are checked directly instead of assuming that
        disappearing from catalogue results means sold.
        """

        try:
            listings = (
                self.database
                .listings_for_status_check(
                    STATUS_CHECK_BATCH_SIZE
                )
            )

        except Exception:
            stats.status_failures += 1

            LOGGER.exception(
                "Unable to load listing status queue"
            )

            return

        if not listings:
            LOGGER.info(
                "No listings waiting for status check"
            )
            return

        LOGGER.info(
            "Checking availability of %d stored listings",
            len(listings),
        )

        for index, stored in enumerate(
            listings
        ):
            if STOP_EVENT.is_set():
                return

            listing_id = str(
                stored["id"]
            )

            url = str(
                stored["url"]
            )

            try:
                result = (
                    self.status_checker.check(
                        url
                    )
                )

                stats.status_checks += 1

                if self._details_present(
                    result.details
                ):
                    self._persist_details(
                        listing_id,
                        result.details,
                    )
                    stats.enriched_listings += 1

                if result.status == ListingStatus.UNKNOWN:
                    stats.status_failures += 1

                    self.database.set_listing_status(
                        listing_id,
                        ListingStatus.UNKNOWN.value,
                        reason=result.reason,
                    )

                    LOGGER.debug(
                        "UNKNOWN STATUS | %s | %s | %s",
                        listing_id,
                        stored["title"],
                        result.reason,
                    )

                    continue

                if result.status == ListingStatus.SOLD:
                    changed = (
                        self.database
                        .set_listing_status(
                            listing_id,
                            ListingStatus.SOLD.value,
                            reason=result.reason,
                        )
                    )

                    if changed:
                        stats.sold_detected += 1

                        LOGGER.info(
                            "SOLD | %s | %s | £%.2f",
                            stored["search_name"],
                            stored["title"],
                            self._stored_price(
                                stored
                            ),
                        )

                elif result.status == ListingStatus.NOT_FOUND:
                    changed = (
                        self.database
                        .set_listing_status(
                            listing_id,
                            ListingStatus.NOT_FOUND.value,
                            reason=result.reason,
                        )
                    )

                    if changed:
                        stats.unavailable_detected += 1

                        LOGGER.info(
                            "NOT FOUND | %s | %s",
                            stored["search_name"],
                            stored["title"],
                        )

                elif result.status == ListingStatus.ACTIVE:
                    changed = (
                        self.database
                        .set_listing_status(
                            listing_id,
                            ListingStatus.ACTIVE.value,
                            reason=result.reason,
                        )
                    )

                    if changed:
                        LOGGER.info(
                            "ACTIVE AGAIN | %s | %s",
                            stored["search_name"],
                            stored["title"],
                        )

            except Exception:
                stats.status_failures += 1

                LOGGER.exception(
                    "Status check failed for listing %s",
                    listing_id,
                )

            if (
                index < len(listings) - 1
                and not STOP_EVENT.is_set()
            ):
                STOP_EVENT.wait(
                    STATUS_CHECK_DELAY
                )

    def _log_cycle_summary(
        self,
        stats: CycleStats,
    ) -> None:
        """Log cycle and sold-market statistics."""

        LOGGER.info("=" * 60)

        LOGGER.info(
            "Listings found    : %d",
            stats.listings_found,
        )

        LOGGER.info(
            "Listings filtered : %d",
            stats.listings_filtered,
        )

        LOGGER.info(
            "New listings      : %d",
            stats.new_listings,
        )

        LOGGER.info(
            "Price drops       : %d",
            stats.price_drops,
        )

        LOGGER.info(
            "Price increases   : %d",
            stats.price_increases,
        )

        LOGGER.info(
            "Failed listings   : %d",
            stats.failed_listings,
        )

        LOGGER.info(
            "Failed searches   : %d",
            stats.failed_searches,
        )

        LOGGER.info(
            "Status checks     : %d",
            stats.status_checks,
        )

        LOGGER.info(
            "Newly sold        : %d",
            stats.sold_detected,
        )

        LOGGER.info(
            "New unavailable   : %d",
            stats.unavailable_detected,
        )

        LOGGER.info(
            "Status failures   : %d",
            stats.status_failures,
        )

        LOGGER.info(
            "Enriched records  : %d",
            stats.enriched_listings,
        )

        LOGGER.info(
            "Telegram sent     : %d",
            stats.notifications_sent,
        )

        LOGGER.info(
            "Telegram failures : %d",
            stats.notification_failures,
        )

        LOGGER.info(
            "Database size     : %d",
            self.database.count(),
        )

        self._log_sold_statistics()

        LOGGER.info("=" * 60)

    def _log_sold_statistics(
        self,
    ) -> None:
        """Log cumulative sold statistics."""

        try:
            sold = (
                self.database.sold_statistics()
            )

            LOGGER.info("-" * 60)
            LOGGER.info(
                "SOLD MARKET STATISTICS"
            )
            LOGGER.info("-" * 60)

            LOGGER.info(
                "Active listings   : %d",
                sold["active"],
            )

            LOGGER.info(
                "Sold listings     : %d",
                sold["sold"],
            )

            LOGGER.info(
                "Sold today        : %d",
                sold["sold_today"],
            )

            LOGGER.info(
                "Sold last 7 days  : %d",
                sold["sold_last_7_days"],
            )

            LOGGER.info(
                "Not found         : %d",
                sold["not_found"],
            )

            LOGGER.info(
                "Unknown           : %d",
                sold["unknown"],
            )

            LOGGER.info(
                "Sell-through rate : %.1f%%",
                sold["sell_through_rate"],
            )

            average_price = (
                sold["average_sold_price"]
            )

            if average_price is None:
                LOGGER.info(
                    "Average sold price: n/a"
                )

            else:
                LOGGER.info(
                    "Average sold price: £%.2f",
                    average_price,
                )

            average_days = (
                sold["average_days_to_sell"]
            )

            if average_days is None:
                LOGGER.info(
                    "Avg days to sell : n/a"
                )

            else:
                LOGGER.info(
                    "Avg days to sell : %.2f",
                    average_days,
                )

            search_stats = (
                self.database
                .sold_statistics_by_search()
            )

            if not search_stats:
                return

            LOGGER.info("-" * 60)
            LOGGER.info(
                "SOLD BY SEARCH"
            )
            LOGGER.info("-" * 60)

            for row in search_stats:
                total = int(
                    row["total"] or 0
                )

                sold_count = int(
                    row["sold"] or 0
                )

                rate = (
                    sold_count
                    / total
                    * 100.0
                    if total
                    else 0.0
                )

                average_sold_price = (
                    row["average_sold_price"]
                )

                if average_sold_price is None:
                    price_text = "n/a"

                else:
                    price_text = (
                        f"£{float(average_sold_price):.2f}"
                    )

                average_days_to_sell = (
                    row["average_days_to_sell"]
                )

                if average_days_to_sell is None:
                    days_text = "n/a"

                else:
                    days_text = (
                        f"{float(average_days_to_sell):.2f}"
                    )

                LOGGER.info(
                    "%s | total=%d | sold=%d | "
                    "rate=%.1f%% | avg=%s | days=%s",
                    row["search_name"]
                    or "Unknown",
                    total,
                    sold_count,
                    rate,
                    price_text,
                    days_text,
                )

        except Exception:
            LOGGER.exception(
                "Unable to calculate sold statistics"
            )

    def close(self) -> None:
        """Cleanly close application resources."""

        LOGGER.info(
            "Stopping Vinted Agent"
        )

        try:
            self.status_checker.close()

        except Exception:
            LOGGER.exception(
                "Unable to close status checker"
            )

        try:
            self.telegram.close()

        except Exception:
            LOGGER.exception(
                "Unable to close Telegram client"
            )

        try:
            self.scraper.stop()

        except Exception:
            LOGGER.exception(
                "Unable to stop browser"
            )

        try:
            self.database.close()

        except Exception:
            LOGGER.exception(
                "Unable to close database"
            )

        LOGGER.info(
            "Vinted Agent stopped"
        )

    def _process_notification_queue(
        self,
        stats: CycleStats,
    ) -> None:
        """
        Deliver pending Telegram notifications.

        Queue rows are durable in SQLite.  Failed sends remain in the queue
        and are retried on the next monitoring cycle instead of being lost.
        """

        try:
            pending = (
                self.database
                .pending_notifications(
                    NOTIFICATION_BATCH_SIZE
                )
            )

        except Exception:
            stats.notification_failures += 1

            LOGGER.exception(
                "Unable to load Telegram notification queue"
            )

            return

        if not pending:
            return

        LOGGER.info(
            "Processing %d queued Telegram notifications",
            len(pending),
        )

        for row in pending:
            if STOP_EVENT.is_set():
                return

            notification_id = int(
                row["id"]
            )

            notification_type = str(
                row["notification_type"]
                or ""
            ).strip()

            try:
                listing = (
                    self._listing_from_notification(
                        row
                    )
                )

                if notification_type == "new_listing":
                    sent = (
                        self.telegram
                        .send_listing(
                            listing
                        )
                    )

                elif notification_type == "price_drop":
                    old_price = float(
                        row["old_price"]
                        or 0.0
                    )

                    sent = (
                        self.telegram
                        .send_price_drop(
                            listing,
                            old_price,
                        )
                    )

                else:
                    raise ValueError(
                        "Unsupported notification type: "
                        f"{notification_type}"
                    )

                if sent:
                    self.database.mark_notification_sent(
                        notification_id
                    )

                    stats.notifications_sent += 1

                    LOGGER.info(
                        "TELEGRAM SENT | %s | %s | attempt=%d",
                        notification_type,
                        listing.id,
                        int(
                            row["attempts"]
                            or 0
                        ) + 1,
                    )

                else:
                    self.database.mark_notification_failed(
                        notification_id,
                        "Telegram API send returned false",
                    )

                    stats.notification_failures += 1

                    LOGGER.warning(
                        "TELEGRAM RETRY PENDING | %s | %s | attempt=%d",
                        notification_type,
                        listing.id,
                        int(
                            row["attempts"]
                            or 0
                        ) + 1,
                    )

            except Exception as exc:
                stats.notification_failures += 1

                LOGGER.exception(
                    "Unable to deliver queued Telegram "
                    "notification %s",
                    notification_id,
                )

                try:
                    self.database.mark_notification_failed(
                        notification_id,
                        str(exc),
                    )

                except Exception:
                    LOGGER.exception(
                        "Unable to record notification failure %s",
                        notification_id,
                    )

    @staticmethod
    def _listing_from_notification(
        row: object,
    ) -> Listing:
        """
        Rebuild the listing snapshot saved with a notification.

        The payload snapshot is preferred so a delayed price-drop alert keeps
        the exact title/price/image values from the event that created it.
        """

        payload: dict[str, object] = {}

        try:
            raw_payload = row[
                "payload_json"
            ]  # type: ignore[index]

            if raw_payload:
                decoded = json.loads(
                    str(raw_payload)
                )

                if isinstance(
                    decoded,
                    dict,
                ):
                    payload = decoded

        except (
            KeyError,
            TypeError,
            ValueError,
            IndexError,
            json.JSONDecodeError,
        ):
            payload = {}

        def value(
            name: str,
            fallback: object = "",
        ) -> object:
            if name in payload:
                return payload[
                    name
                ]

            try:
                stored_value = row[
                    name
                ]  # type: ignore[index]

                if stored_value is not None:
                    return stored_value

            except (
                KeyError,
                TypeError,
                IndexError,
            ):
                pass

            return fallback

        pictures = value(
            "pictures",
            [],
        )

        if not isinstance(
            pictures,
            list,
        ):
            pictures = []

        try:
            queued_listing_id = row[
                "listing_id"
            ]  # type: ignore[index]
        except (
            KeyError,
            TypeError,
            IndexError,
        ):
            queued_listing_id = ""

        return Listing(
            id=str(
                payload.get(
                    "id",
                    queued_listing_id,
                )
                or queued_listing_id
                or ""
            ),
            title=str(
                value(
                    "title",
                    "",
                )
                or ""
            ),
            subtitle=str(
                value(
                    "subtitle",
                    "",
                )
                or ""
            ),
            price=str(
                value(
                    "price",
                    "",
                )
                or ""
            ),
            total_price=str(
                value(
                    "total_price",
                    "",
                )
                or ""
            ),
            url=str(
                value(
                    "url",
                    "",
                )
                or ""
            ),
            image=str(
                value(
                    "image",
                    "",
                )
                or ""
            ),
            search_id=(
                str(
                    value(
                        "search_id",
                        "",
                    )
                )
                or None
            ),
            search_name=(
                str(
                    value(
                        "search_name",
                        "",
                    )
                )
                or None
            ),
            size=(
                str(
                    value(
                        "size",
                        "",
                    )
                )
                or None
            ),
            condition=(
                str(
                    value(
                        "condition",
                        "",
                    )
                )
                or None
            ),
            brand=(
                str(
                    value(
                        "brand",
                        "",
                    )
                )
                or None
            ),
            description=(
                str(
                    value(
                        "description",
                        "",
                    )
                )
                or None
            ),
            pictures=[
                str(
                    picture
                )
                for picture in pictures
                if str(
                    picture
                ).strip()
            ],
            posted_at=(
                str(
                    value(
                        "posted_at",
                        "",
                    )
                )
                or None
            ),
            local_image_path=(
                str(
                    value(
                        "local_image_path",
                        "",
                    )
                )
                or None
            ),
        )

    def _inspect_new_listing(
        self,
        listing: Listing,
        stats: CycleStats,
    ) -> StatusResult | None:
        """Inspect and enrich a newly discovered catalogue listing."""

        try:
            result = self.status_checker.check(
                listing.url
            )

            stats.status_checks += 1

            if self._details_present(
                result.details
            ):
                self._apply_details_to_listing(
                    listing,
                    result.details,
                )
                stats.enriched_listings += 1

            if result.status == ListingStatus.UNKNOWN:
                stats.status_failures += 1

            return result

        except Exception:
            stats.status_failures += 1

            LOGGER.exception(
                "Unable to inspect new listing %s",
                listing.id,
            )

            return None

    def _persist_details(
        self,
        listing_id: str,
        details: ListingDetails,
    ) -> None:
        """Persist item-page enrichment without overwriting useful data."""

        self.database.update_listing_details(
            listing_id,
            size=details.size,
            condition=details.condition,
            brand=details.brand,
            description=details.description,
            pictures=(
                details.pictures
                if details.pictures
                else None
            ),
            posted_at=details.posted_at,
        )

    @staticmethod
    def _apply_details_to_listing(
        listing: Listing,
        details: ListingDetails,
    ) -> None:
        """Copy item-page details onto a Listing before first save."""

        if details.size:
            listing.size = details.size

        if details.condition:
            listing.condition = details.condition

        if details.brand:
            listing.brand = details.brand

        if details.description:
            listing.description = (
                details.description
            )

        if details.pictures:
            listing.pictures = list(
                details.pictures
            )

        if details.posted_at:
            listing.posted_at = (
                details.posted_at
            )

    @staticmethod
    def _details_present(
        details: ListingDetails,
    ) -> bool:
        return any(
            (
                details.size,
                details.condition,
                details.brand,
                details.description,
                details.pictures,
                details.posted_at,
            )
        )

    @staticmethod
    def _search_config(
        search: Search,
    ) -> dict[str, object]:
        """Return the filter configuration used when a listing was found."""

        return {
            "id": search.id,
            "name": search.name,
            "url": search.url,
            "max_price": search.max_price,
            "keywords": list(
                search.keywords
            ),
            "sizes": list(
                search.sizes
            ),
            "conditions": list(
                search.conditions
            ),
        }

    @staticmethod
    def _stored_price(
        stored: object,
    ) -> float:
        """Extract stored numerical price."""

        try:
            value = stored[
                "current_price"
            ]  # type: ignore[index]

            if value is None:
                return 0.0

            return float(value)

        except (
            KeyError,
            TypeError,
            ValueError,
            IndexError,
        ):
            return 0.0


def _handle_shutdown(
    signum: int,
    frame: object,
) -> None:
    """Handle SIGINT/SIGTERM."""

    LOGGER.info(
        "Shutdown signal received: %s",
        signum,
    )

    STOP_EVENT.set()


def main() -> None:
    """Application entry point."""

    configure_logging()

    signal.signal(
        signal.SIGINT,
        _handle_shutdown,
    )

    signal.signal(
        signal.SIGTERM,
        _handle_shutdown,
    )

    agent = VintedAgent()

    try:
        agent.run_forever()

    except KeyboardInterrupt:
        STOP_EVENT.set()

    except Exception:
        LOGGER.exception(
            "Fatal Vinted Agent error"
        )

    finally:
        agent.close()


if __name__ == "__main__":
    main()