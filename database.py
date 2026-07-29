import os
import sqlite3


class Database:

    def __init__(self, db_path="data/listings.db"):

        os.makedirs("data", exist_ok=True)

        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

        self.cursor = self.conn.cursor()

        self.create_tables()

    def create_tables(self):

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS listings (

            id TEXT PRIMARY KEY,

            search_id TEXT,
            search_name TEXT,

            title TEXT,
            subtitle TEXT,

            price TEXT,
            total_price TEXT,

            current_price REAL,
            previous_price REAL,

            url TEXT,
            image TEXT,

            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP
        )
        """)

        self.conn.commit()

        self.upgrade_database()

    def upgrade_database(self):

        self.cursor.execute("PRAGMA table_info(listings)")

        columns = [
            row["name"]
            for row in self.cursor.fetchall()
        ]

        upgrades = {

            "search_id":
                "ALTER TABLE listings ADD COLUMN search_id TEXT",

            "search_name":
                "ALTER TABLE listings ADD COLUMN search_name TEXT",

            "current_price":
                "ALTER TABLE listings ADD COLUMN current_price REAL",

            "previous_price":
                "ALTER TABLE listings ADD COLUMN previous_price REAL",

            "last_seen":
                "ALTER TABLE listings ADD COLUMN last_seen TIMESTAMP"

        }

        for column, sql in upgrades.items():

            if column not in columns:

                print(f"Database upgrade: adding '{column}'")

                self.cursor.execute(sql)

        # Initialise last_seen for older databases
        self.cursor.execute("""
            UPDATE listings
            SET last_seen = first_seen
            WHERE last_seen IS NULL
        """)

        self.conn.commit()

    def get(self, listing_id):

        self.cursor.execute(
            """
            SELECT *
            FROM listings
            WHERE id=?
            """,
            (listing_id,)
        )

        return self.cursor.fetchone()

    def exists(self, listing_id):

        return self.get(listing_id) is not None

    def save(self, listing):

        self.cursor.execute(
            """
            INSERT INTO listings (

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
                image

            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                listing.price_value,

                listing.url,
                listing.image
            )
        )

        self.conn.commit()

    def update_price(self, listing):

        self.cursor.execute(
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
                listing.price_value,

                listing.price,
                listing.total_price,

                listing.id
            )
        )

        self.conn.commit()

    def touch(self, listing_id):

        self.cursor.execute(
            """
            UPDATE listings

            SET last_seen = CURRENT_TIMESTAMP

            WHERE id = ?
            """,
            (listing_id,)
        )

        self.conn.commit()

    def count(self):

        self.cursor.execute(
            "SELECT COUNT(*) FROM listings"
        )

        return self.cursor.fetchone()[0]

    def count_for_search(self, search_name):

        self.cursor.execute(
            """
            SELECT COUNT(*)
            FROM listings
            WHERE search_name = ?
            """,
            (search_name,)
        )

        return self.cursor.fetchone()[0]

    def clear(self):

        self.cursor.execute(
            "DELETE FROM listings"
        )

        self.conn.commit()

    def close(self):

        self.conn.close()