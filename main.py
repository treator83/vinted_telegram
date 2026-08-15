"""Long-running orchestration for Vinted Agent."""

from __future__ import annotations

import logging
import math
import signal
import threading
import time
from dataclasses import dataclass
from types import FrameType
from typing import Final

from config import CHECK_INTERVAL
from database import Database
from filters import allow
from logger import configure_logging
from models import Listing
from scraper import VintedScraper
from search import Search
from search_manager import SearchManager
from telegram_client import TelegramClient

LOGGER = logging.getLogger(__name__)

SEPARATOR: Final[str] = "=" * 60
SUB_SEPARATOR: Final[str] = "-" * 60
PRICE_TOLERANCE: Final[float] = 0.005
MINIMUM_CHECK_INTERVAL: Final[int] = 1

STOP_EVENT = threading.Event()


@dataclass(slots=True)
class CycleStats:
    """Statistics collected during one complete monitoring cycle."""

    listings_found: int = 0
    listings_filtered: int = 0
    new_listings: int = 0
    price_drops: int = 0
    price_increases: int = 0
    failed_listings: int = 0
    failed_searches: int = 0


class VintedAgent:
    """Coordinate searches, scraping, persistence, and notifications."""

    def __init__(
        self,
        database: Database | None = None,
        telegram: TelegramClient | None = None,
        scraper: VintedScraper | None = None,
        search_manager: SearchManager | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.database = database or Database()
        self.telegram = telegram or TelegramClient()
        self.scraper = scraper or VintedScraper()
        self.search_manager = search_manager or SearchManager()
        self.stop_event = stop_event or threading.Event()

        self._started = False
        self._closed = False

    def start(self) -> None:
        """Start resources required by the monitoring service."""

        if self._started:
            return

        LOGGER.info(SEPARATOR)
        LOGGER.info("Starting Vinted Agent")
        LOGGER.info(SEPARATOR)

        self.scraper.start()
        self._started = True

    def run_forever(self) -> None:
        """Run monitoring cycles until shutdown is requested."""

        self.start()

        interval = max(
            MINIMUM_CHECK_INTERVAL,
            int(CHECK_INTERVAL),
        )

        while not self.stop_event.is_set():
            cycle_started = time.monotonic()

            try:
                self.run_cycle()
            except Exception:
                LOGGER.exception("Monitoring cycle failed")

            if self.stop_event.is_set():
                break

            elapsed = time.monotonic() - cycle_started

            LOGGER.info(
                "Cycle completed in %.2f seconds; sleeping for %s seconds",
                elapsed,
                interval,
            )

            self.stop_event.wait(interval)

        LOGGER.info("Vinted Agent shutdown requested")

    def run_cycle(self) -> CycleStats:
        """Execute one complete pass through all configured searches."""

        if not self._started:
            self.start()

        stats = CycleStats()
        searches = self.search_manager.load()

        if not searches:
            LOGGER.warning("No searches are configured")
            return stats

        LOGGER.info(SEPARATOR)
        LOGGER.info(
            "Starting monitoring cycle with %s searches",
            len(searches),
        )
        LOGGER.info(SEPARATOR)

        for search in searches:
            if self.stop_event.is_set():
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

            LOGGER.info(SUB_SEPARATOR)

        self._log_cycle_summary(stats)

        return stats

    def close(self) -> None:
        """Close all application resources safely."""

        if self._closed:
            return

        self._closed = True

        try:
            self.scraper.stop()
        except Exception:
            LOGGER.exception(
                "Unable to stop scraper cleanly"
            )

        try:
            self.telegram.close()
        except Exception:
            LOGGER.exception(
                "Unable to close Telegram client cleanly"
            )

        try:
            self.database.close()
        except Exception:
            LOGGER.exception(
                "Unable to close database cleanly"
            )

        LOGGER.info("Vinted Agent stopped")

    def __enter__(self) -> VintedAgent:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.close()

    def _process_search(
        self,
        search: Search,
        stats: CycleStats,
    ) -> None:
        LOGGER.info(
            "Searching: %s",
            search.name,
        )

        self.scraper.open(search.url)
        self.scraper.accept_cookies()

        listings = self.scraper.fetch()

        stats.listings_found += len(listings)

        LOGGER.info(
            "Found %s listings",
            len(listings),
        )

        search_new = 0

        for listing in listings:
            if self.stop_event.is_set():
                break

            listing.search_id = str(search.id)
            listing.search_name = search.name

            if not allow(listing, search):
                stats.listings_filtered += 1
                continue

            try:
                if self._process_listing(
                    listing,
                    stats,
                ):
                    search_new += 1

            except Exception:
                stats.failed_listings += 1

                LOGGER.exception(
                    "Unable to process listing %s from search %s",
                    listing.id,
                    search.name,
                )

        LOGGER.info(
            "New listings: %s",
            search_new,
        )

    def _process_listing(
        self,
        listing: Listing,
        stats: CycleStats,
    ) -> bool:
        existing = self.database.get(
            listing.id
        )

        if existing is None:
            inserted = self.database.save(
                listing
            )

            if not inserted:
                return False

            notification_sent = (
                self.telegram.send_listing(
                    listing
                )
            )

            if not notification_sent:
                LOGGER.warning(
                    "New-listing notification failed for %s",
                    listing.id,
                )

            LOGGER.info(
                "NEW | %s | %s | %s",
                listing.search_name,
                listing.title,
                listing.price,
            )

            stats.new_listings += 1

            return True

        old_price = self._stored_price(
            existing["current_price"],
            fallback=listing.price_value,
        )

        price_difference = (
            listing.price_value - old_price
        )

        if price_difference < -PRICE_TOLERANCE:
            self.database.update_price(
                listing
            )

            notification_sent = (
                self.telegram.send_price_drop(
                    listing,
                    old_price,
                )
            )

            if not notification_sent:
                LOGGER.warning(
                    "Price-drop notification failed for %s",
                    listing.id,
                )

            LOGGER.info(
                "PRICE DROP | %s | %.2f -> %.2f",
                listing.search_name,
                old_price,
                listing.price_value,
            )

            stats.price_drops += 1

            return False

        if price_difference > PRICE_TOLERANCE:
            self.database.update_price(
                listing
            )

            LOGGER.info(
                "PRICE UPDATE | %s | %.2f -> %.2f",
                listing.search_name,
                old_price,
                listing.price_value,
            )

            stats.price_increases += 1

            return False

        self.database.touch(
            listing.id
        )

        return False

    def _log_cycle_summary(
        self,
        stats: CycleStats,
    ) -> None:
        try:
            database_size = (
                self.database.count()
            )
        except Exception:
            database_size = -1
            LOGGER.exception(
                "Unable to read database size"
            )

        LOGGER.info(SEPARATOR)
        LOGGER.info(
            "Listings found    : %s",
            stats.listings_found,
        )
        LOGGER.info(
            "Listings filtered : %s",
            stats.listings_filtered,
        )
        LOGGER.info(
            "New listings      : %s",
            stats.new_listings,
        )
        LOGGER.info(
            "Price drops       : %s",
            stats.price_drops,
        )
        LOGGER.info(
            "Price increases   : %s",
            stats.price_increases,
        )
        LOGGER.info(
            "Failed listings   : %s",
            stats.failed_listings,
        )
        LOGGER.info(
            "Failed searches   : %s",
            stats.failed_searches,
        )

        if database_size >= 0:
            LOGGER.info(
                "Database size     : %s",
                database_size,
            )

        LOGGER.info(SEPARATOR)

    @staticmethod
    def _stored_price(
        value: object,
        fallback: float,
    ) -> float:
        try:
            price = float(value)
        except (TypeError, ValueError):
            return fallback

        if not math.isfinite(price):
            return fallback

        return price


def install_signal_handlers() -> None:
    """Handle Ctrl+C and systemd termination gracefully."""

    def handle_shutdown(
        signum: int,
        frame: FrameType | None,
    ) -> None:
        del frame

        try:
            signal_name = signal.Signals(
                signum
            ).name
        except ValueError:
            signal_name = str(signum)

        LOGGER.info(
            "Received %s; shutting down",
            signal_name,
        )

        STOP_EVENT.set()

    signal.signal(
        signal.SIGINT,
        handle_shutdown,
    )

    signal.signal(
        signal.SIGTERM,
        handle_shutdown,
    )


def run_once() -> CycleStats:
    """Run one monitoring cycle and close resources afterward."""

    agent = VintedAgent(
        stop_event=STOP_EVENT
    )

    try:
        agent.start()
        return agent.run_cycle()
    finally:
        agent.close()


def main() -> int:
    """Run Vinted Agent as a long-lived service."""

    configure_logging()
    install_signal_handlers()

    agent: VintedAgent | None = None

    try:
        agent = VintedAgent(
            stop_event=STOP_EVENT
        )

        agent.run_forever()

        return 0

    except KeyboardInterrupt:
        STOP_EVENT.set()

        LOGGER.info(
            "Interrupted by user"
        )

        return 0

    except Exception:
        LOGGER.exception(
            "Fatal Vinted Agent error"
        )

        return 1

    finally:
        if agent is not None:
            agent.close()


if __name__ == "__main__":
    raise SystemExit(main())