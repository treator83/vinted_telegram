"""Send hourly Vinted market statistics to Telegram."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import BOT_TOKEN, CHAT_ID, DATABASE_PATH


LOGGER = logging.getLogger(__name__)

UK_TIMEZONE = ZoneInfo("Europe/London")

TELEGRAM_TIMEOUT = 30


def utc_timestamp(
    value: datetime,
) -> str:
    """Convert an aware datetime to SQLite UTC timestamp format."""

    return (
        value
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def get_statistics() -> dict:
    """Read current statistics from the production database."""

    connection = sqlite3.connect(
        str(DATABASE_PATH),
        timeout=30,
    )

    connection.row_factory = sqlite3.Row

    now_local = datetime.now(
        UK_TIMEZONE
    )

    now_utc = now_local.astimezone(
        timezone.utc
    )

    today_local = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    today_utc = today_local.astimezone(
        timezone.utc
    )

    seven_days_ago = (
        now_utc - timedelta(days=7)
    )

    one_hour_ago = (
        now_utc - timedelta(hours=1)
    )

    try:
        totals = connection.execute(
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

        sold_today = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM listings
            WHERE listing_status IN (
                'sold',
                'not_found'
            )
              AND COALESCE(
                    sold_at,
                    status_checked_at
                  ) >= ?
            """,
            (
                utc_timestamp(today_utc),
            ),
        ).fetchone()

        sold_last_7_days = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM listings
            WHERE listing_status IN (
                'sold',
                'not_found'
            )
              AND COALESCE(
                    sold_at,
                    status_checked_at
                  ) >= ?
            """,
            (
                utc_timestamp(
                    seven_days_ago
                ),
            ),
        ).fetchone()

        new_last_hour = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM listings
            WHERE first_seen >= ?
            """,
            (
                utc_timestamp(
                    one_hour_ago
                ),
            ),
        ).fetchone()

        average_days = connection.execute(
            """
            SELECT
                AVG(
                    JULIANDAY(
                        COALESCE(
                            sold_at,
                            status_checked_at
                        )
                    )
                    -
                    JULIANDAY(first_seen)
                ) AS value

            FROM listings

            WHERE listing_status IN (
                'sold',
                'not_found'
            )

              AND COALESCE(
                    sold_at,
                    status_checked_at
                  ) IS NOT NULL

              AND first_seen IS NOT NULL
            """
        ).fetchone()

        by_search = connection.execute(
            """
            SELECT
                COALESCE(
                    search_name,
                    'Unknown'
                ) AS search_name,

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
                ) AS average_sold_price

            FROM listings

            GROUP BY
                search_id,
                search_name

            ORDER BY
                search_name
            """
        ).fetchall()

    finally:
        connection.close()

    total = int(
        totals["total"] or 0
    )

    active = int(
        totals["active"] or 0
    )

    sold = int(
        totals["sold"] or 0
    )

    unknown = int(
        totals["unknown"] or 0
    )

    sell_through_rate = (
        sold / total * 100.0
        if total
        else 0.0
    )

    average_sold_price = (
        float(
            totals["average_sold_price"]
        )
        if totals["average_sold_price"]
        is not None
        else None
    )

    average_days_to_sell = (
        float(
            average_days["value"]
        )
        if average_days["value"]
        is not None
        else None
    )

    return {
        "time": now_local,
        "total": total,
        "active": active,
        "sold": sold,
        "unknown": unknown,
        "sold_today": int(
            sold_today["count"] or 0
        ),
        "sold_last_7_days": int(
            sold_last_7_days["count"] or 0
        ),
        "new_last_hour": int(
            new_last_hour["count"] or 0
        ),
        "average_sold_price": (
            average_sold_price
        ),
        "average_days_to_sell": (
            average_days_to_sell
        ),
        "sell_through_rate": (
            sell_through_rate
        ),
        "by_search": by_search,
    }


def build_message(
    stats: dict,
) -> str:
    """Build Telegram statistics message."""

    report_time = stats[
        "time"
    ].strftime(
        "%d %b %Y %H:%M"
    )

    lines = [
        "📊 VINTED HOURLY STATISTICS",
        "",
        f"🕐 {report_time}",
        "",
        f"📦 Tracked: {stats['total']}",
        f"🟢 Active: {stats['active']}",
        (
            "🔴 Sold: "
            f"{stats['sold']}"
            " (includes unavailable)"
        ),
        f"❓ Unknown: {stats['unknown']}",
        "",
        (
            "🆕 New last hour: "
            f"{stats['new_last_hour']}"
        ),
        (
            "💰 Sold today: "
            f"{stats['sold_today']}"
        ),
        (
            "📅 Sold last 7 days: "
            f"{stats['sold_last_7_days']}"
        ),
        (
            "📈 Sell-through: "
            f"{stats['sell_through_rate']:.1f}%"
        ),
    ]

    if (
        stats["average_sold_price"]
        is not None
    ):
        lines.append(
            "💷 Average sold price: "
            f"£{stats['average_sold_price']:.2f}"
        )

    else:
        lines.append(
            "💷 Average sold price: n/a"
        )

    if (
        stats["average_days_to_sell"]
        is not None
    ):
        lines.append(
            "⏱ Average time to sell: "
            f"{stats['average_days_to_sell']:.1f} days"
        )

    else:
        lines.append(
            "⏱ Average time to sell: n/a"
        )

    lines.extend(
        [
            "",
            "🔎 BY SEARCH",
        ]
    )

    for row in stats["by_search"]:
        total = int(
            row["total"] or 0
        )

        active = int(
            row["active"] or 0
        )

        sold = int(
            row["sold"] or 0
        )

        rate = (
            sold / total * 100.0
            if total
            else 0.0
        )

        average_price = (
            row["average_sold_price"]
        )

        if average_price is None:
            price_text = "n/a"

        else:
            price_text = (
                f"£{float(average_price):.2f}"
            )

        lines.extend(
            [
                "",
                f"🏷 {row['search_name']}",
                (
                    f"  Active: {active}"
                    f" | Sold: {sold}"
                ),
                (
                    f"  Sell-through: "
                    f"{rate:.1f}%"
                ),
                (
                    f"  Avg sold: "
                    f"{price_text}"
                ),
            ]
        )

    return "\n".join(
        lines
    )


def send_telegram(
    message: str,
) -> None:
    """Send the statistics report to Telegram."""

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    if not CHAT_ID:
        raise RuntimeError(
            "CHAT_ID is missing"
        )

    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": "true",
        },
        timeout=TELEGRAM_TIMEOUT,
    )

    response.raise_for_status()

    payload = response.json()

    if not payload.get(
        "ok",
        False,
    ):
        raise RuntimeError(
            "Telegram rejected statistics message"
        )


def main() -> None:
    """Generate and send one report."""

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(message)s"
        ),
    )

    LOGGER.info(
        "Generating Vinted statistics"
    )

    stats = get_statistics()

    message = build_message(
        stats
    )

    send_telegram(
        message
    )

    LOGGER.info(
        "Statistics sent successfully"
    )


if __name__ == "__main__":
    main()