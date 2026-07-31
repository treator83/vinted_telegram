"""SQLite persistence for Vinted listings and price history."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Final

from models import Listing, extract_price

LOGGER = logging.getLogger(__name__)

DEFAULT_DATABASE_PATH: Final[Path] = Path("data/listings.db")
DATABASE_TIMEOUT_SECONDS: Final[float] = 30.0
BUSY_TIMEOUT_MILLISECONDS: Final[int] = 5_000
PRICE_TOLERANCE: Final[float] = 0.005
SCHEMA_VERSION: Final[int] = 2


class DatabaseError(RuntimeError):
    """Raised when a database operation cannot be completed."""


class Database:
    """Store listings and price history in SQLite."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DATABASE_PATH,
    ) -> None:
        self.db_path = str(db_path)
        self._closed = False

        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(
                parents=True,
                exist_ok=True,
            )

        try:
            self.conn = sqlite3.connect(
                self.db_path,
                timeout=DATABASE_TIMEOUT_SECONDS,
            )
            self.conn.row_factory = sqlite3.Row

            # Retained for compatibility with older project code.
            self.cursor = self.conn.cursor()

            self._configure_connection()
            self.create_tables()

            LOGGER.info(
                "Database opened: %s",
                self.db_path,
            )
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to open database {self.db_path}: {exc}"
            ) from exc

    def create_tables(self) -> None:
        """Create and upgrade all required database tables."""

        self._ensure_open()

        try:
            with self.conn:
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS listings (
                        id TEXT PRIMARY KEY,

                        search_id TEXT,
                        search_name TEXT,

                        title TEXT NOT NULL DEFAULT '',
                        subtitle TEXT NOT NULL DEFAULT '',

                        price TEXT NOT NULL DEFAULT '',
                        total_price TEXT NOT NULL DEFAULT '',

                        current_price REAL NOT NULL DEFAULT 0,
                        previous_price REAL NOT NULL DEFAULT 0,

                        url TEXT NOT NULL DEFAULT '',
                        image TEXT NOT NULL DEFAULT '',

                        first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

            self.upgrade_database()
            self._create_price_history_table()
            self._create_indexes()
            self._backfill_existing_rows()
            self._seed_missing_price_history()
            self._set_schema_version()

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to create database schema: {exc}"
            ) from exc

    def upgrade_database(self) -> None:
        """Add columns missing from older database versions."""

        self._ensure_open()

        columns = self._listing_columns()

        if "id" not in columns:
            raise DatabaseError(
                "The listings table does not contain an id column"
            )

        required_columns = {
            "search_id": "TEXT",
            "search_name": "TEXT",
            "title": "TEXT NOT NULL DEFAULT ''",
            "subtitle": "TEXT NOT NULL DEFAULT ''",
            "price": "TEXT NOT NULL DEFAULT ''",
            "total_price": "TEXT NOT NULL DEFAULT ''",
            "current_price": "REAL",
            "previous_price": "REAL",
            "url": "TEXT NOT NULL DEFAULT ''",
            "image": "TEXT NOT NULL DEFAULT ''",
            # SQLite does not allow CURRENT_TIMESTAMP when adding a column
            # through ALTER TABLE, so these are added nullable and backfilled.
            "first_seen": "TEXT",
            "last_seen": "TEXT",
        }

        try:
            with self.conn:
                for column_name, definition in required_columns.items():
                    if column_name in columns:
                        continue

                    LOGGER.info(
                        "Database migration: adding listings.%s",
                        column_name,
                    )

                    self.conn.execute(
                        f"""
                        ALTER TABLE listings
                        ADD COLUMN {column_name} {definition}
                        """
                    )

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to upgrade database schema: {exc}"
            ) from exc

    def get(self, listing_id: str) -> sqlite3.Row | None:
        """Return a stored listing or ``None``."""

        self._ensure_open()

        try:
            return self.conn.execute(
                """
                SELECT *
                FROM listings
                WHERE id = ?
                """,
                (str(listing_id),),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to read listing {listing_id}: {exc}"
            ) from exc

    def exists(self, listing_id: str) -> bool:
        """Return whether a listing is stored."""

        self._ensure_open()

        try:
            row = self.conn.execute(
                """
                SELECT 1
                FROM listings
                WHERE id = ?
                LIMIT 1
                """,
                (str(listing_id),),
            ).fetchone()

            return row is not None
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to check listing {listing_id}: {exc}"
            ) from exc

    def save(self, listing: Listing) -> bool:
        """Insert a listing.

        Returns ``True`` when inserted and ``False`` when the ID already exists.
        """

        self._ensure_open()

        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO listings (
                        id,
                        search_id,
                        search_name,
                        title,
                        subtitle,
                        price,
                        total_price,
                        current_price,
                        previous_price,
                        url,
                        image,
                        first_seen,
                        last_seen
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """,
                    (
                        str(listing.id),
                        listing.search_id,
                        listing.search_name,
                        listing.title,
                        listing.subtitle,
                        listing.price,
                        listing.total_price,
                        listing.price_value,
                        listing.price_value,
                        listing.url,
                        listing.image,
                    ),
                )

                inserted = cursor.rowcount > 0

                if inserted:
                    self._record_price(
                        listing_id=str(listing.id),
                        price=listing.price_value,
                    )

            return inserted

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to save listing {listing.id}: {exc}"
            ) from exc

    def update_price(self, listing: Listing) -> bool:
        """Update listing information and record a changed price.

        Returns ``True`` when the numeric price changed.
        """

        self._ensure_open()

        existing = self.get(str(listing.id))

        if existing is None:
            return self.save(listing)

        old_price = self._safe_price(
            existing["current_price"],
            fallback=listing.price_value,
        )
        price_changed = (
            abs(old_price - listing.price_value) >= PRICE_TOLERANCE
        )

        try:
            with self.conn:
                if price_changed:
                    self.conn.execute(
                        """
                        UPDATE listings
                        SET
                            search_id = ?,
                            search_name = ?,
                            title = ?,
                            subtitle = ?,
                            price = ?,
                            total_price = ?,
                            previous_price = current_price,
                            current_price = ?,
                            url = ?,
                            image = ?,
                            last_seen = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            listing.search_id,
                            listing.search_name,
                            listing.title,
                            listing.subtitle,
                            listing.price,
                            listing.total_price,
                            listing.price_value,
                            listing.url,
                            listing.image,
                            str(listing.id),
                        ),
                    )

                    self._record_price(
                        listing_id=str(listing.id),
                        price=listing.price_value,
                    )
                else:
                    self.conn.execute(
                        """
                        UPDATE listings
                        SET
                            search_id = ?,
                            search_name = ?,
                            title = ?,
                            subtitle = ?,
                            price = ?,
                            total_price = ?,
                            url = ?,
                            image = ?,
                            last_seen = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            listing.search_id,
                            listing.search_name,
                            listing.title,
                            listing.subtitle,
                            listing.price,
                            listing.total_price,
                            listing.url,
                            listing.image,
                            str(listing.id),
                        ),
                    )

            return price_changed

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to update listing {listing.id}: {exc}"
            ) from exc

    def touch(self, listing_id: str) -> bool:
        """Update a listing's last-seen timestamp."""

        self._ensure_open()

        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    UPDATE listings
                    SET last_seen = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (str(listing_id),),
                )

            return cursor.rowcount > 0

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to touch listing {listing_id}: {exc}"
            ) from exc

    def count(self) -> int:
        """Return the total number of listings."""

        self._ensure_open()

        try:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM listings
                """
            ).fetchone()

            return int(row["total"]) if row else 0

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to count listings: {exc}"
            ) from exc

    def count_for_search(self, search_name: str) -> int:
        """Return the number of listings associated with a search."""

        self._ensure_open()

        try:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM listings
                WHERE search_name = ?
                """,
                (search_name,),
            ).fetchone()

            return int(row["total"]) if row else 0

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to count listings for {search_name}: {exc}"
            ) from exc

    def price_history(
        self,
        listing_id: str,
    ) -> list[sqlite3.Row]:
        """Return recorded prices for a listing, oldest first."""

        self._ensure_open()

        try:
            rows = self.conn.execute(
                """
                SELECT price, observed_at
                FROM price_history
                WHERE listing_id = ?
                ORDER BY observed_at ASC, id ASC
                """,
                (str(listing_id),),
            ).fetchall()

            return list(rows)

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to read price history for {listing_id}: {exc}"
            ) from exc

    def recent_listings(
        self,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        """Return recently observed listings."""

        self._ensure_open()
        safe_limit = max(1, int(limit))

        try:
            return list(
                self.conn.execute(
                    """
                    SELECT *
                    FROM listings
                    ORDER BY last_seen DESC, first_seen DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            )

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to read recent listings: {exc}"
            ) from exc

    def search_statistics(self) -> list[sqlite3.Row]:
        """Return listing statistics grouped by search."""

        self._ensure_open()

        try:
            return list(
                self.conn.execute(
                    """
                    SELECT
                        search_id,
                        search_name,
                        COUNT(*) AS listing_count,
                        MIN(current_price) AS minimum_price,
                        MAX(current_price) AS maximum_price,
                        AVG(current_price) AS average_price,
                        MAX(last_seen) AS last_seen
                    FROM listings
                    GROUP BY search_id, search_name
                    ORDER BY listing_count DESC, search_name ASC
                    """
                ).fetchall()
            )

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to read search statistics: {exc}"
            ) from exc

    def clear(self) -> None:
        """Delete all listings and price-history rows."""

        self._ensure_open()

        try:
            with self.conn:
                self.conn.execute("DELETE FROM price_history")
                self.conn.execute("DELETE FROM listings")

            LOGGER.warning("All database data was deleted")

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to clear database: {exc}"
            ) from exc

    def close(self) -> None:
        """Close the database connection safely."""

        if self._closed:
            return

        self._closed = True

        try:
            self.cursor.close()
        except sqlite3.Error:
            pass

        try:
            self.conn.close()
            LOGGER.info("Database closed")
        except sqlite3.Error as exc:
            LOGGER.warning(
                "Database did not close cleanly: %s",
                exc,
            )

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.close()

    def _configure_connection(self) -> None:
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}"
        )

        if self.db_path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")

    def _create_price_history_table(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_id TEXT NOT NULL,
                    price REAL NOT NULL,
                    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (listing_id)
                        REFERENCES listings(id)
                        ON DELETE CASCADE
                )
                """
            )

    def _create_indexes(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_listings_search_name
                ON listings(search_name)
                """
            )
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_listings_last_seen
                ON listings(last_seen)
                """
            )
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_listings_current_price
                ON listings(current_price)
                """
            )
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_price_history_listing
                ON price_history(listing_id, observed_at)
                """
            )

    def _backfill_existing_rows(self) -> None:
        rows = self.conn.execute(
            """
            SELECT
                id,
                price,
                current_price,
                previous_price,
                first_seen,
                last_seen
            FROM listings
            """
        ).fetchall()

        with self.conn:
            for row in rows:
                current_price = self._safe_price(
                    row["current_price"],
                    fallback=extract_price(row["price"]),
                )
                previous_price = self._safe_price(
                    row["previous_price"],
                    fallback=current_price,
                )

                first_seen = row["first_seen"] or None
                last_seen = row["last_seen"] or first_seen

                self.conn.execute(
                    """
                    UPDATE listings
                    SET
                        current_price = ?,
                        previous_price = ?,
                        first_seen = COALESCE(?, CURRENT_TIMESTAMP),
                        last_seen = COALESCE(?, ?, CURRENT_TIMESTAMP)
                    WHERE id = ?
                    """,
                    (
                        current_price,
                        previous_price,
                        first_seen,
                        last_seen,
                        first_seen,
                        row["id"],
                    ),
                )

    def _seed_missing_price_history(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO price_history (
                    listing_id,
                    price,
                    observed_at
                )
                SELECT
                    listings.id,
                    listings.current_price,
                    COALESCE(
                        listings.first_seen,
                        CURRENT_TIMESTAMP
                    )
                FROM listings
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM price_history
                    WHERE price_history.listing_id = listings.id
                )
                """
            )

    def _record_price(
        self,
        listing_id: str,
        price: float,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO price_history (
                listing_id,
                price,
                observed_at
            )
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (
                listing_id,
                float(price),
            ),
        )

    def _listing_columns(self) -> set[str]:
        rows = self.conn.execute(
            "PRAGMA table_info(listings)"
        ).fetchall()

        return {str(row["name"]) for row in rows}

    def _set_schema_version(self) -> None:
        self.conn.execute(
            f"PRAGMA user_version = {SCHEMA_VERSION}"
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise DatabaseError(
                "Database connection is closed"
            )

    @staticmethod
    def _safe_price(
        value: object,
        fallback: float,
    ) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(fallback)