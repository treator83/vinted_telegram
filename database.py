"""SQLite persistence for Vinted Agent."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from models import Listing

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 3


class DatabaseError(RuntimeError):
    """Raised when a database operation cannot be completed."""


class Database:
    """Store listings, prices, availability status, and statistics."""

    def __init__(self, filename: str | Path = "data/listings.db") -> None:
        self.filename = Path(filename)

        if str(filename) != ":memory:":
            self.filename.parent.mkdir(parents=True, exist_ok=True)

        try:
            self.connection = sqlite3.connect(
                str(filename),
                timeout=30,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to open database: {filename}"
            ) from exc

        self.connection.row_factory = sqlite3.Row

        self._configure()
        self.create_tables()
        self.upgrade_database()

        LOGGER.info("Database opened: %s", filename)

    def _configure(self) -> None:
        """Configure SQLite for reliable long-running operation."""

        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 10000")

            if str(self.filename) != ":memory:":
                self.connection.execute("PRAGMA journal_mode = WAL")
                self.connection.execute("PRAGMA synchronous = NORMAL")

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to configure database: {exc}"
            ) from exc

    def create_tables(self) -> None:
        """Create the current database schema."""

        try:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    id TEXT PRIMARY KEY,

                    search_id TEXT,
                    search_name TEXT,

                    title TEXT NOT NULL,
                    subtitle TEXT,

                    price TEXT,
                    total_price TEXT,

                    current_price REAL,
                    previous_price REAL,

                    url TEXT NOT NULL,
                    image TEXT,

                    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                    listing_status TEXT NOT NULL DEFAULT 'active',
                    sold_at TEXT,
                    status_checked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    listing_id TEXT NOT NULL,
                    price REAL NOT NULL,

                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (listing_id)
                        REFERENCES listings(id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_listings_search_name
                    ON listings(search_name);

                CREATE INDEX IF NOT EXISTS idx_listings_last_seen
                    ON listings(last_seen);

                CREATE INDEX IF NOT EXISTS idx_listings_current_price
                    ON listings(current_price);

                CREATE INDEX IF NOT EXISTS idx_listings_status
                    ON listings(listing_status);

                CREATE INDEX IF NOT EXISTS idx_listings_status_checked
                    ON listings(status_checked_at);

                CREATE INDEX IF NOT EXISTS idx_listings_sold_at
                    ON listings(sold_at);

                CREATE INDEX IF NOT EXISTS idx_price_history_listing
                    ON price_history(listing_id);

                CREATE INDEX IF NOT EXISTS idx_price_history_time
                    ON price_history(recorded_at);
                """
            )

            self.connection.commit()

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to create database tables: {exc}"
            ) from exc

    def upgrade_database(self) -> None:
        """Upgrade databases created by older versions of the agent."""

        try:
            columns = self._listing_columns()

            additions = {
                "search_id": "TEXT",
                "search_name": "TEXT",
                "current_price": "REAL",
                "previous_price": "REAL",
                "first_seen": "TEXT",
                "last_seen": "TEXT",
                "listing_status": "TEXT DEFAULT 'active'",
                "sold_at": "TEXT",
                "status_checked_at": "TEXT",
            }

            for column, definition in additions.items():
                if column not in columns:
                    LOGGER.info(
                        "Adding database column: listings.%s",
                        column,
                    )

                    self.connection.execute(
                        f"ALTER TABLE listings "
                        f"ADD COLUMN {column} {definition}"
                    )

            self.connection.execute(
                """
                UPDATE listings
                SET first_seen = CURRENT_TIMESTAMP
                WHERE first_seen IS NULL
                """
            )

            self.connection.execute(
                """
                UPDATE listings
                SET last_seen = COALESCE(first_seen, CURRENT_TIMESTAMP)
                WHERE last_seen IS NULL
                """
            )

            self.connection.execute(
                """
                UPDATE listings
                SET listing_status = 'active'
                WHERE listing_status IS NULL
                   OR TRIM(listing_status) = ''
                """
            )

            rows = self.connection.execute(
                """
                SELECT id, price, current_price
                FROM listings
                """
            ).fetchall()

            for row in rows:
                if row["current_price"] is None:
                    price = self._safe_price(row["price"])

                    self.connection.execute(
                        """
                        UPDATE listings
                        SET current_price = ?
                        WHERE id = ?
                        """,
                        (price, row["id"]),
                    )

            self._seed_missing_price_history()

            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_listings_status
                ON listings(listing_status)
                """
            )

            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_listings_status_checked
                ON listings(status_checked_at)
                """
            )

            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_listings_sold_at
                ON listings(sold_at)
                """
            )

            self.connection.execute(
                f"PRAGMA user_version = {SCHEMA_VERSION}"
            )

            self.connection.commit()

        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to upgrade database: {exc}"
            ) from exc

    def get(self, listing_id: str) -> sqlite3.Row | None:
        """Return one stored listing."""

        try:
            return self.connection.execute(
                """
                SELECT *
                FROM listings
                WHERE id = ?
                """,
                (listing_id,),
            ).fetchone()

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to load listing {listing_id}: {exc}"
            ) from exc

    def exists(self, listing_id: str) -> bool:
        """Return True if the listing already exists."""

        try:
            row = self.connection.execute(
                """
                SELECT 1
                FROM listings
                WHERE id = ?
                LIMIT 1
                """,
                (listing_id,),
            ).fetchone()

            return row is not None

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to check listing {listing_id}: {exc}"
            ) from exc

    def save(self, listing: Listing) -> bool:
        """
        Save a new listing.

        Returns True when inserted and False when it already existed.
        """

        try:
            cursor = self.connection.execute(
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
                    last_seen,
                    listing_status
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    'active'
                )
                """,
                (
                    listing.id,
                    listing.search_id,
                    listing.search_name,
                    listing.title,
                    listing.subtitle,
                    listing.price,
                    listing.total_price,
                    listing.price_value,
                    listing.url,
                    listing.image,
                ),
            )

            inserted = cursor.rowcount > 0

            if inserted:
                self.connection.execute(
                    """
                    INSERT INTO price_history (
                        listing_id,
                        price
                    )
                    VALUES (?, ?)
                    """,
                    (
                        listing.id,
                        listing.price_value,
                    ),
                )

                self.connection.commit()

            return inserted

        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to save listing {listing.id}: {exc}"
            ) from exc

    def update_price(
        self,
        listing_id: str,
        new_price: float,
    ) -> bool:
        """
        Update a stored listing price.

        Returns True only when the numerical price actually changed.
        """

        try:
            row = self.connection.execute(
                """
                SELECT current_price
                FROM listings
                WHERE id = ?
                """,
                (listing_id,),
            ).fetchone()

            if row is None:
                return False

            old_price = self._safe_price(row["current_price"])
            new_price = self._safe_price(new_price)

            if abs(old_price - new_price) < 0.005:
                return False

            self.connection.execute(
                """
                UPDATE listings
                SET
                    previous_price = current_price,
                    current_price = ?,
                    last_seen = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    new_price,
                    listing_id,
                ),
            )

            self.connection.execute(
                """
                INSERT INTO price_history (
                    listing_id,
                    price
                )
                VALUES (?, ?)
                """,
                (
                    listing_id,
                    new_price,
                ),
            )

            self.connection.commit()

            return True

        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to update price for {listing_id}: {exc}"
            ) from exc

    def touch(self, listing_id: str) -> None:
        """
        Mark a listing as seen in the latest catalogue scrape.

        If a previously unavailable listing appears again, restore it to
        active unless it was explicitly confirmed as sold.
        """

        try:
            self.connection.execute(
                """
                UPDATE listings
                SET
                    last_seen = CURRENT_TIMESTAMP,
                    listing_status =
                        CASE
                            WHEN listing_status = 'sold'
                                THEN listing_status
                            ELSE 'active'
                        END
                WHERE id = ?
                """,
                (listing_id,),
            )

            self.connection.commit()

        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to touch listing {listing_id}: {exc}"
            ) from exc

    def set_listing_status(
        self,
        listing_id: str,
        status: str,
    ) -> bool:
        """
        Store the latest verified availability state.

        Returns True when the listing changed to a different status.
        """

        allowed = {
            "active",
            "sold",
            "not_found",
            "unknown",
        }

        status = status.strip().lower()

        if status not in allowed:
            raise ValueError(
                f"Unsupported listing status: {status}"
            )

        try:
            row = self.connection.execute(
                """
                SELECT listing_status
                FROM listings
                WHERE id = ?
                """,
                (listing_id,),
            ).fetchone()

            if row is None:
                return False

            previous_status = (
                row["listing_status"] or "active"
            ).strip().lower()

            changed = previous_status != status

            if status == "sold":
                self.connection.execute(
                    """
                    UPDATE listings
                    SET
                        listing_status = 'sold',
                        sold_at = COALESCE(
                            sold_at,
                            CURRENT_TIMESTAMP
                        ),
                        status_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (listing_id,),
                )

            elif status == "active":
                self.connection.execute(
                    """
                    UPDATE listings
                    SET
                        listing_status = 'active',
                        sold_at = NULL,
                        status_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (listing_id,),
                )

            else:
                self.connection.execute(
                    """
                    UPDATE listings
                    SET
                        listing_status = ?,
                        status_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        status,
                        listing_id,
                    ),
                )

            self.connection.commit()

            return changed

        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to update listing status for "
                f"{listing_id}: {exc}"
            ) from exc

    def listings_for_status_check(
        self,
        limit: int = 10,
    ) -> list[sqlite3.Row]:
        """
        Return listings that should be checked for sale status.

        The oldest status checks are returned first.
        """

        limit = max(1, int(limit))

        try:
            return self.connection.execute(
                """
                SELECT
                    id,
                    search_id,
                    search_name,
                    title,
                    current_price,
                    url,
                    listing_status,
                    first_seen,
                    last_seen,
                    status_checked_at
                FROM listings
                WHERE listing_status != 'sold'
                ORDER BY
                    CASE
                        WHEN status_checked_at IS NULL THEN 0
                        ELSE 1
                    END,
                    status_checked_at ASC,
                    first_seen ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to load status-check queue: {exc}"
            ) from exc

    def count(self) -> int:
        """Return total number of stored listings."""

        try:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM listings
                """
            ).fetchone()

            return int(row["count"])

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to count listings: {exc}"
            ) from exc

    def count_for_search(
        self,
        search_id: str,
    ) -> int:
        """Return total listings stored for a configured search."""

        try:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM listings
                WHERE search_id = ?
                """,
                (search_id,),
            ).fetchone()

            return int(row["count"])

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to count search {search_id}: {exc}"
            ) from exc

    def count_by_status(
        self,
        status: str,
    ) -> int:
        """Return number of listings with a given status."""

        try:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM listings
                WHERE listing_status = ?
                """,
                (status,),
            ).fetchone()

            return int(row["count"])

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to count status {status}: {exc}"
            ) from exc

    def price_history(
        self,
        listing_id: str,
    ) -> list[sqlite3.Row]:
        """Return chronological price history for a listing."""

        try:
            return self.connection.execute(
                """
                SELECT
                    price,
                    recorded_at
                FROM price_history
                WHERE listing_id = ?
                ORDER BY recorded_at ASC, id ASC
                """,
                (listing_id,),
            ).fetchall()

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to load price history for "
                f"{listing_id}: {exc}"
            ) from exc

    def recent_listings(
        self,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        """Return recently discovered listings."""

        try:
            return self.connection.execute(
                """
                SELECT *
                FROM listings
                ORDER BY first_seen DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to load recent listings: {exc}"
            ) from exc

    def sold_statistics(self) -> dict[str, Any]:
        """Return overall sold-market statistics."""

        try:
            totals = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(
                        CASE
                            WHEN listing_status = 'active'
                            THEN 1
                            ELSE 0
                        END
                    ) AS active,
                    SUM(
                        CASE
                            WHEN listing_status = 'sold'
                            THEN 1
                            ELSE 0
                        END
                    ) AS sold,
                    SUM(
                        CASE
                            WHEN listing_status = 'not_found'
                            THEN 1
                            ELSE 0
                        END
                    ) AS not_found,
                    SUM(
                        CASE
                            WHEN listing_status = 'unknown'
                            THEN 1
                            ELSE 0
                        END
                    ) AS unknown,
                    AVG(
                        CASE
                            WHEN listing_status = 'sold'
                            THEN current_price
                        END
                    ) AS average_sold_price
                FROM listings
                """
            ).fetchone()

            sold_today = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM listings
                WHERE listing_status = 'sold'
                  AND sold_at IS NOT NULL
                  AND DATE(sold_at) = DATE('now')
                """
            ).fetchone()["count"]

            sold_week = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM listings
                WHERE listing_status = 'sold'
                  AND sold_at IS NOT NULL
                  AND sold_at >= DATETIME(
                      'now',
                      '-7 days'
                  )
                """
            ).fetchone()["count"]

            average_days = self.connection.execute(
                """
                SELECT AVG(
                    JULIANDAY(sold_at) -
                    JULIANDAY(first_seen)
                ) AS value
                FROM listings
                WHERE listing_status = 'sold'
                  AND sold_at IS NOT NULL
                  AND first_seen IS NOT NULL
                """
            ).fetchone()["value"]

            total = int(totals["total"] or 0)
            sold = int(totals["sold"] or 0)

            sell_through_rate = (
                (sold / total) * 100.0
                if total
                else 0.0
            )

            return {
                "total": total,
                "active": int(totals["active"] or 0),
                "sold": sold,
                "not_found": int(
                    totals["not_found"] or 0
                ),
                "unknown": int(
                    totals["unknown"] or 0
                ),
                "sold_today": int(sold_today or 0),
                "sold_last_7_days": int(
                    sold_week or 0
                ),
                "average_sold_price": (
                    float(totals["average_sold_price"])
                    if totals["average_sold_price"]
                    is not None
                    else None
                ),
                "average_days_to_sell": (
                    float(average_days)
                    if average_days is not None
                    else None
                ),
                "sell_through_rate": sell_through_rate,
            }

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to calculate sold statistics: {exc}"
            ) from exc

    def sold_statistics_by_search(
        self,
    ) -> list[sqlite3.Row]:
        """Return sold statistics grouped by configured search."""

        try:
            return self.connection.execute(
                """
                SELECT
                    search_id,
                    search_name,
                    COUNT(*) AS total,
                    SUM(
                        CASE
                            WHEN listing_status = 'active'
                            THEN 1
                            ELSE 0
                        END
                    ) AS active,
                    SUM(
                        CASE
                            WHEN listing_status = 'sold'
                            THEN 1
                            ELSE 0
                        END
                    ) AS sold,
                    AVG(
                        CASE
                            WHEN listing_status = 'sold'
                            THEN current_price
                        END
                    ) AS average_sold_price,
                    AVG(
                        CASE
                            WHEN listing_status = 'sold'
                             AND sold_at IS NOT NULL
                             AND first_seen IS NOT NULL
                            THEN
                                JULIANDAY(sold_at) -
                                JULIANDAY(first_seen)
                        END
                    ) AS average_days_to_sell
                FROM listings
                GROUP BY
                    search_id,
                    search_name
                ORDER BY search_name
                """
            ).fetchall()

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to calculate search sold "
                f"statistics: {exc}"
            ) from exc

    def search_statistics(self) -> list[sqlite3.Row]:
        """Return basic statistics grouped by configured search."""

        try:
            return self.connection.execute(
                """
                SELECT
                    search_id,
                    search_name,
                    COUNT(*) AS listing_count,
                    MIN(current_price) AS minimum_price,
                    MAX(current_price) AS maximum_price,
                    AVG(current_price) AS average_price,
                    SUM(
                        CASE
                            WHEN listing_status = 'sold'
                            THEN 1
                            ELSE 0
                        END
                    ) AS sold_count
                FROM listings
                GROUP BY
                    search_id,
                    search_name
                ORDER BY search_name
                """
            ).fetchall()

        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to calculate search statistics: {exc}"
            ) from exc

    def clear(self) -> None:
        """Delete all stored listings and price history."""

        try:
            self.connection.execute(
                "DELETE FROM price_history"
            )

            self.connection.execute(
                "DELETE FROM listings"
            )

            self.connection.commit()

            LOGGER.warning("All database data was deleted")

        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to clear database: {exc}"
            ) from exc

    def close(self) -> None:
        """Close the database connection."""

        try:
            self.connection.close()
            LOGGER.info("Database closed")

        except sqlite3.Error as exc:
            LOGGER.warning(
                "Error while closing database: %s",
                exc,
            )

    def _listing_columns(self) -> set[str]:
        rows = self.connection.execute(
            "PRAGMA table_info(listings)"
        ).fetchall()

        return {
            str(row["name"])
            for row in rows
        }

    def _seed_missing_price_history(self) -> None:
        self.connection.execute(
            """
            INSERT INTO price_history (
                listing_id,
                price,
                recorded_at
            )
            SELECT
                listings.id,
                listings.current_price,
                COALESCE(
                    listings.first_seen,
                    CURRENT_TIMESTAMP
                )
            FROM listings
            WHERE listings.current_price IS NOT NULL
              AND NOT EXISTS (
                    SELECT 1
                    FROM price_history
                    WHERE
                        price_history.listing_id =
                        listings.id
              )
            """
        )

    @staticmethod
    def _safe_price(value: Any) -> float:
        if value is None:
            return 0.0

        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip()

        if not text:
            return 0.0

        cleaned = "".join(
            character
            for character in text
            if character.isdigit()
            or character in {".", ","}
        )

        if not cleaned:
            return 0.0

        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")

        try:
            return float(cleaned)

        except ValueError:
            return 0.0

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.close()