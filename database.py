"""SQLite persistence for Vinted Agent."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from models import Listing


LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 5


class DatabaseError(RuntimeError):
    """Raised when a database operation cannot be completed."""


class Database:
    """Store permanent Vinted listing records and delivery state."""

    def __init__(
        self,
        filename: str | Path = "data/listings.db",
    ) -> None:
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
        try:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    id TEXT PRIMARY KEY,

                    search_id TEXT,
                    search_name TEXT,
                    search_config_hash TEXT,
                    search_config_json TEXT,

                    title TEXT NOT NULL DEFAULT '',
                    subtitle TEXT NOT NULL DEFAULT '',

                    size TEXT,
                    item_condition TEXT,
                    brand TEXT,
                    description TEXT,

                    price TEXT NOT NULL DEFAULT '',
                    total_price TEXT NOT NULL DEFAULT '',
                    current_price REAL NOT NULL DEFAULT 0,
                    previous_price REAL NOT NULL DEFAULT 0,

                    url TEXT NOT NULL DEFAULT '',

                    image TEXT NOT NULL DEFAULT '',
                    pictures_json TEXT NOT NULL DEFAULT '[]',
                    local_image_path TEXT,

                    posted_at TEXT,
                    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                    listing_status TEXT NOT NULL DEFAULT 'active',
                    status_reason TEXT,
                    sold_at TEXT,
                    status_checked_at TEXT,

                    notified_at TEXT
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

                CREATE TABLE IF NOT EXISTS notification_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    listing_id TEXT NOT NULL,
                    notification_type TEXT NOT NULL,

                    old_price REAL,
                    payload_json TEXT,

                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,

                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_attempt_at TEXT,
                    sent_at TEXT,

                    UNIQUE(listing_id, notification_type, created_at),

                    FOREIGN KEY (listing_id)
                        REFERENCES listings(id)
                        ON DELETE CASCADE
                );
                """
            )

            self.connection.commit()
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to create database tables: {exc}"
            ) from exc

    def upgrade_database(self) -> None:
        try:
            columns = self._listing_columns()

            additions = {
                "search_id": "TEXT",
                "search_name": "TEXT",
                "current_price": "REAL NOT NULL DEFAULT 0",
                "previous_price": "REAL NOT NULL DEFAULT 0",
                "first_seen": "TEXT",
                "last_seen": "TEXT",
                "listing_status": "TEXT NOT NULL DEFAULT 'active'",
                "sold_at": "TEXT",
                "status_checked_at": "TEXT",
                "size": "TEXT",
                "item_condition": "TEXT",
                "pictures_json": "TEXT NOT NULL DEFAULT '[]'",
                "posted_at": "TEXT",

                # Schema v5.
                "brand": "TEXT",
                "description": "TEXT",
                "local_image_path": "TEXT",
                "status_reason": "TEXT",
                "search_config_hash": "TEXT",
                "search_config_json": "TEXT",
                "notified_at": "TEXT",
            }

            for column, definition in additions.items():
                if column in columns:
                    continue

                LOGGER.info(
                    "Adding database column: listings.%s",
                    column,
                )

                self.connection.execute(
                    f"ALTER TABLE listings ADD COLUMN {column} {definition}"
                )

            self._upgrade_price_history()

            self.connection.execute(
                """
                UPDATE listings
                SET first_seen = CURRENT_TIMESTAMP
                WHERE first_seen IS NULL
                   OR TRIM(first_seen) = ''
                """
            )

            self.connection.execute(
                """
                UPDATE listings
                SET last_seen = COALESCE(first_seen, CURRENT_TIMESTAMP)
                WHERE last_seen IS NULL
                   OR TRIM(last_seen) = ''
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

            self._backfill_current_prices()
            self._backfill_picture_lists()
            self._seed_missing_price_history()
            self._create_indexes()

            self.connection.execute(
                f"PRAGMA user_version = {SCHEMA_VERSION}"
            )

            self.connection.commit()

            LOGGER.info(
                "Database schema version: %d",
                SCHEMA_VERSION,
            )
        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to upgrade database: {exc}"
            ) from exc

    def _upgrade_price_history(self) -> None:
        rows = self.connection.execute(
            "PRAGMA table_info(price_history)"
        ).fetchall()

        columns = {
            str(row["name"])
            for row in rows
        }

        if (
            "observed_at" in columns
            and "recorded_at" not in columns
        ):
            LOGGER.info(
                "Migrating price_history.observed_at to recorded_at"
            )

            self.connection.execute(
                """
                ALTER TABLE price_history
                RENAME COLUMN observed_at TO recorded_at
                """
            )
            return

        if (
            "recorded_at" not in columns
            and "observed_at" not in columns
        ):
            LOGGER.info("Adding price_history.recorded_at")

            self.connection.execute(
                """
                ALTER TABLE price_history
                ADD COLUMN recorded_at TEXT
                """
            )

            self.connection.execute(
                """
                UPDATE price_history
                SET recorded_at = CURRENT_TIMESTAMP
                WHERE recorded_at IS NULL
                """
            )

    def _create_indexes(self) -> None:
        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS
                idx_listings_search_id
            ON listings(search_id);

            CREATE INDEX IF NOT EXISTS
                idx_listings_search_name
            ON listings(search_name);

            CREATE INDEX IF NOT EXISTS
                idx_listings_status
            ON listings(listing_status);

            CREATE INDEX IF NOT EXISTS
                idx_listings_first_seen
            ON listings(first_seen);

            CREATE INDEX IF NOT EXISTS
                idx_listings_last_seen
            ON listings(last_seen);

            CREATE INDEX IF NOT EXISTS
                idx_listings_sold_at
            ON listings(sold_at);

            CREATE INDEX IF NOT EXISTS
                idx_listings_posted_at
            ON listings(posted_at);

            CREATE INDEX IF NOT EXISTS
                idx_listings_status_checked
            ON listings(status_checked_at);

            CREATE INDEX IF NOT EXISTS
                idx_listings_size
            ON listings(size);

            CREATE INDEX IF NOT EXISTS
                idx_listings_condition
            ON listings(item_condition);

            CREATE INDEX IF NOT EXISTS
                idx_listings_brand
            ON listings(brand);

            CREATE INDEX IF NOT EXISTS
                idx_price_history_listing
            ON price_history(listing_id);

            CREATE INDEX IF NOT EXISTS
                idx_price_history_recorded
            ON price_history(recorded_at);

            CREATE INDEX IF NOT EXISTS
                idx_notification_queue_state
            ON notification_queue(state, created_at);

            CREATE INDEX IF NOT EXISTS
                idx_notification_queue_listing
            ON notification_queue(listing_id);
            """
        )

    def get(
        self,
        listing_id: str,
    ) -> sqlite3.Row | None:
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

    def exists(
        self,
        listing_id: str,
    ) -> bool:
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

    def save(
        self,
        listing: Listing,
        *,
        search_config: dict[str, Any] | None = None,
    ) -> bool:
        pictures_json = self._pictures_json(
            listing.pictures,
            listing.image,
        )

        search_config_json = (
            self._canonical_json(search_config)
            if search_config is not None
            else None
        )

        search_config_hash = (
            self._config_hash(search_config_json)
            if search_config_json is not None
            else None
        )

        try:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO listings (
                    id,
                    search_id,
                    search_name,
                    search_config_hash,
                    search_config_json,
                    title,
                    subtitle,
                    size,
                    item_condition,
                    brand,
                    description,
                    price,
                    total_price,
                    current_price,
                    previous_price,
                    url,
                    image,
                    pictures_json,
                    local_image_path,
                    posted_at,
                    first_seen,
                    last_seen,
                    listing_status,
                    status_reason
                )
                VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, 0,
                    ?, ?, ?, ?, ?,
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP,
                    'active',
                    'catalogue'
                )
                """,
                (
                    listing.id,
                    listing.search_id,
                    listing.search_name,
                    search_config_hash,
                    search_config_json,
                    listing.title,
                    listing.subtitle,
                    listing.size,
                    listing.condition,
                    listing.brand,
                    listing.description,
                    listing.price,
                    listing.total_price,
                    listing.price_value,
                    listing.url,
                    listing.image,
                    pictures_json,
                    listing.local_image_path,
                    listing.posted_at,
                ),
            )

            inserted = cursor.rowcount > 0

            if inserted:
                self.connection.execute(
                    """
                    INSERT INTO price_history (
                        listing_id,
                        price,
                        recorded_at
                    )
                    VALUES (?, ?, CURRENT_TIMESTAMP)
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

    def update_listing_details(
        self,
        listing_id: str,
        *,
        size: str | None = None,
        condition: str | None = None,
        brand: str | None = None,
        description: str | None = None,
        pictures: Iterable[str] | None = None,
        posted_at: str | None = None,
        local_image_path: str | None = None,
    ) -> bool:
        row = self.get(listing_id)

        if row is None:
            return False

        picture_json: str | None = None

        if pictures is not None:
            picture_json = self._pictures_json(
                pictures,
                row["image"],
            )

        try:
            self.connection.execute(
                """
                UPDATE listings
                SET
                    size = COALESCE(NULLIF(?, ''), size),
                    item_condition = COALESCE(NULLIF(?, ''), item_condition),
                    brand = COALESCE(NULLIF(?, ''), brand),
                    description = COALESCE(NULLIF(?, ''), description),

                    pictures_json =
                        CASE
                            WHEN ? IS NULL
                            THEN pictures_json
                            ELSE ?
                        END,

                    posted_at = COALESCE(NULLIF(?, ''), posted_at),
                    local_image_path =
                        COALESCE(NULLIF(?, ''), local_image_path)

                WHERE id = ?
                """,
                (
                    size,
                    condition,
                    brand,
                    description,
                    picture_json,
                    picture_json,
                    posted_at,
                    local_image_path,
                    listing_id,
                ),
            )

            self.connection.commit()
            return True
        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to update listing details for "
                f"{listing_id}: {exc}"
            ) from exc

    def update_price(
        self,
        listing: Listing | str,
        new_price: float | None = None,
    ) -> bool:
        if isinstance(listing, Listing):
            listing_id = listing.id
            price_value = listing.price_value
            price_text: str | None = listing.price
            total_price_text: str | None = listing.total_price
        else:
            listing_id = str(listing)

            if new_price is None:
                raise ValueError(
                    "new_price is required when listing ID is supplied"
                )

            price_value = float(new_price)
            price_text = None
            total_price_text = None

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

            old_price = self._safe_price(
                row["current_price"]
            )

            price_value = self._safe_price(
                price_value
            )

            if abs(old_price - price_value) < 0.005:
                return False

            if price_text is None:
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
                        price_value,
                        listing_id,
                    ),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE listings
                    SET
                        previous_price = current_price,
                        current_price = ?,
                        price = ?,
                        total_price = ?,
                        last_seen = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        price_value,
                        price_text,
                        total_price_text,
                        listing_id,
                    ),
                )

            self.connection.execute(
                """
                INSERT INTO price_history (
                    listing_id,
                    price,
                    recorded_at
                )
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    listing_id,
                    price_value,
                ),
            )

            self.connection.commit()
            return True
        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to update price for {listing_id}: {exc}"
            ) from exc

    def touch(
        self,
        listing_id: str,
    ) -> None:
        """Mark a catalogue-visible listing active again."""

        try:
            self.connection.execute(
                """
                UPDATE listings
                SET
                    last_seen = CURRENT_TIMESTAMP,
                    listing_status = 'active',
                    status_reason = 'catalogue',
                    sold_at = NULL
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
        *,
        reason: str | None = None,
    ) -> bool:
        status = status.strip().lower()

        allowed = {
            "active",
            "sold",
            "not_found",
            "unknown",
        }

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
                row["listing_status"]
                or "active"
            ).strip().lower()

            changed = previous_status != status

            if status in {
                "sold",
                "not_found",
            }:
                self.connection.execute(
                    """
                    UPDATE listings
                    SET
                        listing_status = ?,
                        status_reason = ?,
                        sold_at = COALESCE(
                            sold_at,
                            CURRENT_TIMESTAMP
                        ),
                        status_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        status,
                        reason,
                        listing_id,
                    ),
                )
            elif status == "active":
                self.connection.execute(
                    """
                    UPDATE listings
                    SET
                        listing_status = 'active',
                        status_reason = ?,
                        sold_at = NULL,
                        status_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        reason,
                        listing_id,
                    ),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE listings
                    SET
                        listing_status = 'unknown',
                        status_reason = ?,
                        status_checked_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        reason,
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
        limit = max(
            1,
            int(limit),
        )

        try:
            return self.connection.execute(
                """
                SELECT
                    id,
                    search_id,
                    search_name,
                    title,
                    subtitle,
                    size,
                    item_condition,
                    brand,
                    description,
                    current_price,
                    url,
                    image,
                    pictures_json,
                    local_image_path,
                    posted_at,
                    listing_status,
                    status_reason,
                    first_seen,
                    last_seen,
                    sold_at,
                    status_checked_at
                FROM listings
                WHERE listing_status NOT IN (
                    'sold',
                    'not_found'
                )
                ORDER BY
                    CASE
                        WHEN status_checked_at IS NULL
                        THEN 0
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
                f"Unable to load status queue: {exc}"
            ) from exc

    def enqueue_notification(
        self,
        listing_id: str,
        notification_type: str,
        *,
        old_price: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Queue a Telegram notification for retryable delivery."""

        notification_type = str(
            notification_type
        ).strip()

        if not notification_type:
            raise ValueError(
                "notification_type cannot be empty"
            )

        payload_json = (
            self._canonical_json(payload)
            if payload is not None
            else None
        )

        try:
            cursor = self.connection.execute(
                """
                INSERT INTO notification_queue (
                    listing_id,
                    notification_type,
                    old_price,
                    payload_json,
                    state
                )
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (
                    listing_id,
                    notification_type,
                    old_price,
                    payload_json,
                ),
            )

            self.connection.commit()
            return int(cursor.lastrowid)
        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to queue notification for "
                f"{listing_id}: {exc}"
            ) from exc

    def pending_notifications(
        self,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        try:
            return self.connection.execute(
                """
                SELECT
                    notification_queue.*,
                    listings.title,
                    listings.subtitle,
                    listings.price,
                    listings.total_price,
                    listings.current_price,
                    listings.url,
                    listings.image,
                    listings.search_id,
                    listings.search_name
                FROM notification_queue
                JOIN listings
                  ON listings.id = notification_queue.listing_id
                WHERE notification_queue.state IN (
                    'pending',
                    'failed'
                )
                ORDER BY notification_queue.created_at ASC
                LIMIT ?
                """,
                (
                    max(
                        1,
                        int(limit),
                    ),
                ),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to load notification queue: {exc}"
            ) from exc

    def mark_notification_sent(
        self,
        notification_id: int,
    ) -> None:
        try:
            self.connection.execute(
                """
                UPDATE notification_queue
                SET
                    state = 'sent',
                    attempts = attempts + 1,
                    last_attempt_at = CURRENT_TIMESTAMP,
                    sent_at = CURRENT_TIMESTAMP,
                    last_error = NULL
                WHERE id = ?
                """,
                (int(notification_id),),
            )

            self.connection.execute(
                """
                UPDATE listings
                SET notified_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT listing_id
                    FROM notification_queue
                    WHERE id = ?
                )
                """,
                (int(notification_id),),
            )

            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to mark notification "
                f"{notification_id} sent: {exc}"
            ) from exc

    def mark_notification_failed(
        self,
        notification_id: int,
        error: str,
    ) -> None:
        try:
            self.connection.execute(
                """
                UPDATE notification_queue
                SET
                    state = 'failed',
                    attempts = attempts + 1,
                    last_attempt_at = CURRENT_TIMESTAMP,
                    last_error = ?
                WHERE id = ?
                """,
                (
                    str(error)[:1000],
                    int(notification_id),
                ),
            )

            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to mark notification "
                f"{notification_id} failed: {exc}"
            ) from exc

    def listing_records(
        self,
        *,
        sold_only: bool = False,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT
                id,
                search_id,
                search_name,
                search_config_hash,
                title AS name,
                size,
                item_condition AS condition,
                brand,
                description,
                current_price AS price,
                image,
                pictures_json,
                local_image_path,
                url,
                posted_at,
                first_seen,
                last_seen,
                sold_at,
                listing_status,
                status_reason
            FROM listings
        """

        parameters: list[Any] = []

        if sold_only:
            sql += """
                WHERE listing_status IN (
                    'sold',
                    'not_found'
                )
            """

        sql += " ORDER BY first_seen DESC"

        if limit is not None:
            sql += " LIMIT ?"

            parameters.append(
                max(
                    1,
                    int(limit),
                )
            )

        try:
            return self.connection.execute(
                sql,
                parameters,
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to load listing records: {exc}"
            ) from exc

    def count(self) -> int:
        try:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM listings
                """
            ).fetchone()

            return int(row["count"] or 0)
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to count listings: {exc}"
            ) from exc

    def count_for_search(
        self,
        search: str,
    ) -> int:
        try:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM listings
                WHERE search_id = ?
                   OR search_name = ?
                """,
                (
                    search,
                    search,
                ),
            ).fetchone()

            return int(row["count"] or 0)
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to count search {search}: {exc}"
            ) from exc

    def count_by_status(
        self,
        status: str,
    ) -> int:
        try:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM listings
                WHERE listing_status = ?
                """,
                (status,),
            ).fetchone()

            return int(row["count"] or 0)
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to count status {status}: {exc}"
            ) from exc

    def price_history(
        self,
        listing_id: str,
    ) -> list[sqlite3.Row]:
        try:
            return self.connection.execute(
                """
                SELECT
                    price,
                    recorded_at
                FROM price_history
                WHERE listing_id = ?
                ORDER BY
                    recorded_at ASC,
                    id ASC
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
        try:
            return self.connection.execute(
                """
                SELECT *
                FROM listings
                ORDER BY first_seen DESC
                LIMIT ?
                """,
                (
                    max(
                        1,
                        int(limit),
                    ),
                ),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to load recent listings: {exc}"
            ) from exc

    def sold_statistics(self) -> dict[str, Any]:
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
                            WHEN listing_status IN (
                                'sold',
                                'not_found'
                            )
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
                            WHEN listing_status IN (
                                'sold',
                                'not_found'
                            )
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
                WHERE listing_status IN (
                    'sold',
                    'not_found'
                )
                  AND sold_at IS NOT NULL
                  AND DATE(sold_at) = DATE('now')
                """
            ).fetchone()

            sold_week = self.connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM listings
                WHERE listing_status IN (
                    'sold',
                    'not_found'
                )
                  AND sold_at IS NOT NULL
                  AND sold_at >= DATETIME(
                      'now',
                      '-7 days'
                  )
                """
            ).fetchone()

            average_days = self.connection.execute(
                """
                SELECT
                    AVG(
                        JULIANDAY(sold_at)
                        -
                        JULIANDAY(
                            COALESCE(
                                posted_at,
                                first_seen
                            )
                        )
                    ) AS value
                FROM listings
                WHERE listing_status IN (
                    'sold',
                    'not_found'
                )
                  AND sold_at IS NOT NULL
                """
            ).fetchone()

            total = int(totals["total"] or 0)
            sold = int(totals["sold"] or 0)

            sell_through_rate = (
                sold / total * 100.0
                if total
                else 0.0
            )

            return {
                "total": total,
                "active": int(totals["active"] or 0),
                "sold": sold,
                "not_found": int(totals["not_found"] or 0),
                "unknown": int(totals["unknown"] or 0),
                "sold_today": int(sold_today["count"] or 0),
                "sold_last_7_days": int(sold_week["count"] or 0),
                "average_sold_price": (
                    float(totals["average_sold_price"])
                    if totals["average_sold_price"] is not None
                    else None
                ),
                "average_days_to_sell": (
                    float(average_days["value"])
                    if average_days["value"] is not None
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
                            WHEN listing_status IN (
                                'sold',
                                'not_found'
                            )
                            THEN 1
                            ELSE 0
                        END
                    ) AS sold,

                    AVG(
                        CASE
                            WHEN listing_status IN (
                                'sold',
                                'not_found'
                            )
                            THEN current_price
                        END
                    ) AS average_sold_price,

                    AVG(
                        CASE
                            WHEN listing_status IN (
                                'sold',
                                'not_found'
                            )
                             AND sold_at IS NOT NULL
                            THEN
                                JULIANDAY(sold_at)
                                -
                                JULIANDAY(
                                    COALESCE(
                                        posted_at,
                                        first_seen
                                    )
                                )
                        END
                    ) AS average_days_to_sell

                FROM listings
                GROUP BY
                    search_id,
                    search_name
                ORDER BY
                    search_name
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to calculate sold statistics "
                f"by search: {exc}"
            ) from exc

    def search_statistics(
        self,
    ) -> list[sqlite3.Row]:
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
                            WHEN listing_status IN (
                                'sold',
                                'not_found'
                            )
                            THEN 1
                            ELSE 0
                        END
                    ) AS sold_count

                FROM listings
                GROUP BY
                    search_id,
                    search_name
                ORDER BY
                    search_name
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(
                f"Unable to calculate search statistics: {exc}"
            ) from exc

    def clear(self) -> None:
        try:
            self.connection.execute(
                "DELETE FROM notification_queue"
            )
            self.connection.execute(
                "DELETE FROM price_history"
            )
            self.connection.execute(
                "DELETE FROM listings"
            )
            self.connection.commit()

            LOGGER.warning(
                "All database data was deleted"
            )
        except sqlite3.Error as exc:
            self.connection.rollback()

            raise DatabaseError(
                f"Unable to clear database: {exc}"
            ) from exc

    def close(self) -> None:
        try:
            self.connection.close()
            LOGGER.info("Database closed")
        except sqlite3.Error as exc:
            LOGGER.warning(
                "Error closing database: %s",
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

    def _backfill_current_prices(self) -> None:
        rows = self.connection.execute(
            """
            SELECT
                id,
                price,
                current_price
            FROM listings
            """
        ).fetchall()

        for row in rows:
            current_price = self._safe_price(
                row["current_price"]
            )

            if current_price > 0:
                continue

            parsed_price = self._safe_price(
                row["price"]
            )

            if parsed_price <= 0:
                continue

            self.connection.execute(
                """
                UPDATE listings
                SET current_price = ?
                WHERE id = ?
                """,
                (
                    parsed_price,
                    row["id"],
                ),
            )

    def _backfill_picture_lists(self) -> None:
        rows = self.connection.execute(
            """
            SELECT
                id,
                image,
                pictures_json
            FROM listings
            """
        ).fetchall()

        for row in rows:
            existing = (
                row["pictures_json"]
                or ""
            ).strip()

            if existing not in {
                "",
                "[]",
            }:
                continue

            image = (
                row["image"]
                or ""
            ).strip()

            picture_json = (
                "[]"
                if not image
                else json.dumps(
                    [image],
                    ensure_ascii=False,
                )
            )

            self.connection.execute(
                """
                UPDATE listings
                SET pictures_json = ?
                WHERE id = ?
                """,
                (
                    picture_json,
                    row["id"],
                ),
            )

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
                    WHERE price_history.listing_id =
                          listings.id
              )
            """
        )

    @staticmethod
    def _pictures_json(
        pictures: Iterable[str] | None,
        fallback_image: str | None,
    ) -> str:
        result: list[str] = []

        if pictures is not None:
            for picture in pictures:
                value = str(picture).strip()

                if value and value not in result:
                    result.append(value)

        fallback = (
            fallback_image or ""
        ).strip()

        if fallback and fallback not in result:
            result.insert(
                0,
                fallback,
            )

        return json.dumps(
            result,
            ensure_ascii=False,
        )

    @staticmethod
    def _canonical_json(
        value: Any,
    ) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _config_hash(
        config_json: str,
    ) -> str:
        return hashlib.sha256(
            config_json.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _safe_price(
        value: Any,
    ) -> float:
        if value is None:
            return 0.0

        if isinstance(
            value,
            (int, float),
        ):
            return float(value)

        text = str(value).strip()

        if not text:
            return 0.0

        cleaned = "".join(
            character
            for character in text
            if (
                character.isdigit()
                or character in {
                    ".",
                    ",",
                }
            )
        )

        if not cleaned:
            return 0.0

        if (
            "," in cleaned
            and "." in cleaned
        ):
            cleaned = cleaned.replace(
                ",",
                "",
            )
        elif "," in cleaned:
            cleaned = cleaned.replace(
                ",",
                ".",
            )

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
