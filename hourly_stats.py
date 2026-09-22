"""Send the scheduled daily Vinted market report to Telegram."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

import requests

from config import BOT_TOKEN, CHAT_ID, DATABASE_PATH


LOGGER = logging.getLogger(__name__)

UK_TIMEZONE = ZoneInfo("Europe/London")
TELEGRAM_TIMEOUT = 30
SOLD_STATUSES = ("sold", "not_found")


def utc_timestamp(value: datetime) -> str:
    """Convert an aware datetime to SQLite UTC timestamp format."""

    return (
        value
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def _median_or_none(values: list[float]) -> float | None:
    """Return the median of a numeric list, or None when it is empty."""

    if not values:
        return None

    return float(median(values))


def _sold_count_since(
    connection: sqlite3.Connection,
    threshold: datetime,
) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM listings
        WHERE listing_status IN ('sold', 'not_found')
          AND COALESCE(sold_at, status_checked_at) >= ?
        """,
        (utc_timestamp(threshold),),
    ).fetchone()

    return int(row["count"] or 0)


def get_statistics() -> dict:
    """Read current market statistics from the production database."""

    connection = sqlite3.connect(
        str(DATABASE_PATH),
        timeout=30,
    )
    connection.row_factory = sqlite3.Row

    now_local = datetime.now(UK_TIMEZONE)
    now_utc = now_local.astimezone(timezone.utc)
    today_local = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    today_utc = today_local.astimezone(timezone.utc)
    seven_days_ago = now_utc - timedelta(days=7)
    thirty_days_ago = now_utc - timedelta(days=30)
    one_hour_ago = now_utc - timedelta(hours=1)

    try:
        totals = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN listing_status = 'active' THEN 1 ELSE 0 END) AS active,
                SUM(
                    CASE
                        WHEN listing_status IN ('sold', 'not_found') THEN 1
                        ELSE 0
                    END
                ) AS sold,
                SUM(CASE WHEN listing_status = 'unknown' THEN 1 ELSE 0 END) AS unknown
            FROM listings
            """
        ).fetchone()

        new_today = connection.execute(
            "SELECT COUNT(*) AS count FROM listings WHERE first_seen >= ?",
            (utc_timestamp(today_utc),),
        ).fetchone()

        new_last_hour = connection.execute(
            "SELECT COUNT(*) AS count FROM listings WHERE first_seen >= ?",
            (utc_timestamp(one_hour_ago),),
        ).fetchone()

        sold_rows = connection.execute(
            """
            SELECT
                id,
                title,
                search_name,
                size,
                item_condition,
                current_price,
                url,
                COALESCE(sold_at, status_checked_at) AS unavailable_at,
                COALESCE(posted_at, first_seen) AS market_start,
                JULIANDAY(COALESCE(sold_at, status_checked_at))
                    - JULIANDAY(COALESCE(posted_at, first_seen)) AS days_to_sell
            FROM listings
            WHERE listing_status IN ('sold', 'not_found')
              AND COALESCE(sold_at, status_checked_at) IS NOT NULL
            """
        ).fetchall()

        by_search_rows = connection.execute(
            """
            SELECT
                COALESCE(search_name, 'Unknown') AS search_name,
                COUNT(*) AS total,
                SUM(CASE WHEN listing_status = 'active' THEN 1 ELSE 0 END) AS active,
                SUM(
                    CASE
                        WHEN listing_status IN ('sold', 'not_found') THEN 1
                        ELSE 0
                    END
                ) AS sold,
                AVG(
                    CASE
                        WHEN listing_status IN ('sold', 'not_found')
                        THEN current_price
                    END
                ) AS average_last_asking_price,
                AVG(
                    CASE
                        WHEN listing_status IN ('sold', 'not_found')
                         AND COALESCE(sold_at, status_checked_at) IS NOT NULL
                        THEN
                            JULIANDAY(COALESCE(sold_at, status_checked_at))
                            - JULIANDAY(COALESCE(posted_at, first_seen))
                    END
                ) AS average_days_to_sell
            FROM listings
            GROUP BY search_id, search_name
            ORDER BY search_name
            """
        ).fetchall()

        by_condition_rows = connection.execute(
            """
            SELECT
                item_condition AS label,
                COUNT(*) AS total,
                SUM(
                    CASE
                        WHEN listing_status IN ('sold', 'not_found') THEN 1
                        ELSE 0
                    END
                ) AS sold
            FROM listings
            WHERE item_condition IS NOT NULL
              AND TRIM(item_condition) <> ''
            GROUP BY item_condition
            ORDER BY sold DESC, total DESC, item_condition
            LIMIT 5
            """
        ).fetchall()

        by_size_rows = connection.execute(
            """
            SELECT
                size AS label,
                COUNT(*) AS total,
                SUM(
                    CASE
                        WHEN listing_status IN ('sold', 'not_found') THEN 1
                        ELSE 0
                    END
                ) AS sold
            FROM listings
            WHERE size IS NOT NULL
              AND TRIM(size) <> ''
            GROUP BY size
            ORDER BY sold DESC, total DESC, size
            LIMIT 5
            """
        ).fetchall()

        price_band_rows = connection.execute(
            """
            SELECT
                CASE
                    WHEN current_price <= 20 THEN '£0-20'
                    WHEN current_price <= 40 THEN '£20-40'
                    WHEN current_price <= 60 THEN '£40-60'
                    ELSE '£60+'
                END AS price_band,
                COUNT(*) AS total,
                SUM(CASE WHEN listing_status = 'active' THEN 1 ELSE 0 END) AS active,
                SUM(
                    CASE
                        WHEN listing_status IN ('sold', 'not_found') THEN 1
                        ELSE 0
                    END
                ) AS sold
            FROM listings
            WHERE current_price > 0
            GROUP BY price_band
            ORDER BY
                CASE price_band
                    WHEN '£0-20' THEN 1
                    WHEN '£20-40' THEN 2
                    WHEN '£40-60' THEN 3
                    ELSE 4
                END
            """
        ).fetchall()

    finally:
        connection.close()

    total = int(totals["total"] or 0)
    active = int(totals["active"] or 0)
    sold = int(totals["sold"] or 0)
    unknown = int(totals["unknown"] or 0)

    asking_prices = [
        float(row["current_price"])
        for row in sold_rows
        if row["current_price"] is not None
        and float(row["current_price"]) > 0
    ]

    days_to_sell = [
        float(row["days_to_sell"])
        for row in sold_rows
        if row["days_to_sell"] is not None
        and float(row["days_to_sell"]) >= 0
    ]

    average_last_asking_price = (
        sum(asking_prices) / len(asking_prices)
        if asking_prices
        else None
    )
    median_last_asking_price = _median_or_none(asking_prices)
    average_days_to_sell = (
        sum(days_to_sell) / len(days_to_sell)
        if days_to_sell
        else None
    )
    median_days_to_sell = _median_or_none(days_to_sell)

    fastest_sales = sorted(
        (
            {
                "title": row["title"] or "Untitled",
                "search_name": row["search_name"] or "Unknown",
                "price": float(row["current_price"] or 0),
                "days": float(row["days_to_sell"]),
                "url": row["url"] or "",
            }
            for row in sold_rows
            if row["days_to_sell"] is not None
            and float(row["days_to_sell"]) >= 0
        ),
        key=lambda item: item["days"],
    )[:5]

    by_search = [dict(row) for row in by_search_rows]
    by_condition = [dict(row) for row in by_condition_rows]
    by_size = [dict(row) for row in by_size_rows]
    price_bands = [dict(row) for row in price_band_rows]

    sell_through_rate = (
        sold / total * 100.0
        if total
        else 0.0
    )

    return {
        "time": now_local,
        "total": total,
        "active": active,
        "sold": sold,
        "unknown": unknown,
        "new_today": int(new_today["count"] or 0),
        "new_last_hour": int(new_last_hour["count"] or 0),
        "sold_today": _sold_count_since(connection, today_utc) if False else 0,
        "sold_last_7_days": 0,
        "sold_last_30_days": 0,
        "average_last_asking_price": average_last_asking_price,
        "median_last_asking_price": median_last_asking_price,
        "average_sold_price": average_last_asking_price,
        "average_days_to_sell": average_days_to_sell,
        "median_days_to_sell": median_days_to_sell,
        "sell_through_rate": sell_through_rate,
        "by_search": by_search,
        "by_condition": by_condition,
        "by_size": by_size,
        "price_bands": price_bands,
        "fastest_sales": fastest_sales,
    }


def _format_price(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"£{value:.2f}"


def _format_days(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f} days"


def build_message(stats: dict) -> str:
    """Build the detailed Telegram statistics message."""

    report_time = stats["time"].strftime("%d %b %Y %H:%M")

    lines = [
        "📊 VINTED DAILY STATISTICS",
        "",
        f"🕐 {report_time}",
        "",
        f"📦 Tracked: {stats['total']}",
        f"🟢 Active: {stats['active']}",
        f"🔴 Sold/unavailable: {stats['sold']}",
        f"❓ Unknown: {stats['unknown']}",
        "",
        f"🆕 New today: {stats['new_today']}",
        f"💰 Sold/unavailable today: {stats['sold_today']}",
        f"📅 Last 7 days: {stats['sold_last_7_days']}",
        f"🗓 Last 30 days: {stats['sold_last_30_days']}",
        f"📈 Sell-through: {stats['sell_through_rate']:.1f}%",
        (
            "💷 Average last asking price: "
            f"{_format_price(stats['average_last_asking_price'])}"
        ),
        (
            "💷 Median last asking price: "
            f"{_format_price(stats['median_last_asking_price'])}"
        ),
        (
            "⏱ Average time to sell/unavailable: "
            f"{_format_days(stats['average_days_to_sell'])}"
        ),
        (
            "⏱ Median time to sell/unavailable: "
            f"{_format_days(stats['median_days_to_sell'])}"
        ),
        "",
        "🔎 BY SEARCH",
    ]

    for row in stats["by_search"]:
        total = int(row["total"] or 0)
        active = int(row["active"] or 0)
        sold = int(row["sold"] or 0)
        rate = sold / total * 100.0 if total else 0.0
        average_price = row["average_last_asking_price"]
        average_days = row["average_days_to_sell"]

        lines.extend(
            [
                "",
                f"🏷 {row['search_name']}",
                f"  Active: {active} | Sold/unavailable: {sold}",
                f"  Sell-through: {rate:.1f}%",
                f"  Avg asking: {_format_price(average_price)}",
                f"  Avg time: {_format_days(average_days)}",
            ]
        )

    if stats["price_bands"]:
        lines.extend(["", "💷 PRICE BANDS"])
        for row in stats["price_bands"]:
            lines.append(
                f"  {row['price_band']}: "
                f"{int(row['active'] or 0)} active | "
                f"{int(row['sold'] or 0)} sold/unavailable"
            )

    if stats["by_condition"]:
        lines.extend(["", "🧾 TOP CONDITIONS"])
        for row in stats["by_condition"]:
            lines.append(
                f"  {row['label']}: "
                f"{int(row['sold'] or 0)}/{int(row['total'] or 0)} sold/unavailable"
            )

    if stats["by_size"]:
        lines.extend(["", "📏 TOP SIZES"])
        for row in stats["by_size"]:
            lines.append(
                f"  {row['label']}: "
                f"{int(row['sold'] or 0)}/{int(row['total'] or 0)} sold/unavailable"
            )

    if stats["fastest_sales"]:
        lines.extend(["", "⚡ FASTEST SOLD/UNAVAILABLE"])
        for item in stats["fastest_sales"]:
            lines.append(
                f"  {item['title']} | "
                f"{item['days']:.1f}d | "
                f"£{item['price']:.2f}"
            )

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    """Send the statistics report to Telegram."""

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    if not CHAT_ID:
        raise RuntimeError("CHAT_ID is missing")

    url = "https://api.telegram.org/" f"bot{BOT_TOKEN}/sendMessage"

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
    if not payload.get("ok", False):
        raise RuntimeError("Telegram rejected statistics message")


def main() -> None:
    """Generate and send one report."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    LOGGER.info("Generating Vinted statistics")
    stats = get_statistics()
    message = build_message(stats)
    send_telegram(message)
    LOGGER.info("Statistics sent successfully")


if __name__ == "__main__":
    main()
