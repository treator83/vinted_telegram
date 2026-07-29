import time

from config import CHECK_INTERVAL
from database import Database
from filters import allow
from logger import Logger
from scraper import VintedScraper
from search_manager import SearchManager
from telegram_client import TelegramClient


def run_once():

    db = Database()
    telegram = TelegramClient()
    logger = Logger()

    searches = SearchManager().load()

    scraper = VintedScraper()

    total_found = 0
    total_new = 0
    total_price_drops = 0

    try:

        logger.log("=" * 60)
        logger.log("Starting Vinted Agent")
        logger.log("=" * 60)

        scraper.start()

        for search in searches:

            logger.log(f"Searching: {search.name}")

            scraper.open(search.url)
            scraper.accept_cookies()

            listings = scraper.fetch()

            logger.log(f"Found {len(listings)} listings")

            total_found += len(listings)

            search_new = 0

            for listing in listings:

                listing.search_id = search.id
                listing.search_name = search.name

                if not allow(listing, search):
                    continue

                existing = db.get(listing.id)

                # ------------------------
                # Brand new listing
                # ------------------------

                if existing is None:

                    db.save(listing)

                    telegram.send_listing(listing)

                    logger.log(
                        f"NEW | {search.name} | {listing.title} | {listing.price}"
                    )

                    total_new += 1
                    search_new += 1

                    continue

                # ------------------------
                # Existing listing
                # ------------------------

                db.touch(listing.id)

                old_price = existing["current_price"]

                if old_price is None:
                    old_price = listing.price_value

                # ------------------------
                # Price drop
                # ------------------------

                if listing.price_value < old_price:

                    db.update_price(listing)

                    telegram.send_price_drop(
                        listing,
                        old_price
                    )

                    logger.log(
                        f"PRICE DROP | {search.name} | "
                        f"{old_price:.2f} -> {listing.price_value:.2f}"
                    )

                    total_price_drops += 1

                # ------------------------
                # Price changed (up)
                # ------------------------

                elif listing.price_value > old_price:

                    db.update_price(listing)

                    logger.log(
                        f"PRICE UPDATE | {search.name} | "
                        f"{old_price:.2f} -> {listing.price_value:.2f}"
                    )

            logger.log(f"New listings: {search_new}")
            logger.log("-" * 60)

        logger.log(f"Listings checked : {total_found}")
        logger.log(f"New listings     : {total_new}")
        logger.log(f"Price drops      : {total_price_drops}")
        logger.log(f"Database size    : {db.count()}")

        logger.log("=" * 60)

    except Exception as e:

        logger.log(f"ERROR: {e}")

    finally:

        scraper.stop()
        db.close()


if __name__ == "__main__":

    while True:

        run_once()

        print()
        print(f"Sleeping {CHECK_INTERVAL} seconds...")
        print()

        time.sleep(CHECK_INTERVAL)