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


def utc_timestamp(value: datetime) -> str:
    """Convert an aware datetime to SQLite UTC timestamp format."""

    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _median_or_none(values: list[float]) -> float | None:
    """Return a median value, or None for an empty list."""

    return float(median(values)) if values else None


def get_statistics() -> dict:
    """Read current statistics from the production database."""

    connection = sqlite3.connect(str(DATABASE_PATH), timeout=30)
    connection.row_factory = sqlite3.Row

    now_local = datetime.now(UK_TIMEZONE)
    now_utc = now_local.astimezone(timezone.utc)
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = today_local.astimezone(timezone.utc)
    seven_days_ago = now_utc - timedelta(days=7)
    thirty_days_ago = now_utc - timedelta(days=30)

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

        sold_today = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM listings
            WHERE listing_status IN ('sold', 'not_found')
              AND COALESCE(sold_at, status_checked_at) >= ?
            """,
            (utc_timestamp(today_utc),),
        ).fetchone()

        sold_last_7_days = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM listings
            WHERE listing_status IN ('sold', 'not_found')
              AND COALESCE(sold_at, status_checked_at) >= ?
            """,
            (utc_timestamp(seven_days_ago),),
        ).fetchone()

        sold_last_30_days = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM listings
            WHERE listing_status IN ('sold', 'not_found')
              AND COALESCE(sold_at, status_checked_at) >= ?
            """,
            (utc_timestamp(thirty_days_ago),),
        ).fetchone()

        sold_rows = connection.execute(
            """
            SELECT
                current_price,
                JULIANDAY(COALESCE(sold_at, status_checked_at))
                    - JULIANDAY(COALESCE(posted_at, first_seen)) AS days_to_sell
            FROM listings
            WHERE listing_status IN ('sold', 'not_found')
              AND COALESCE(sold_at, status_checked_at) IS NOT NULL
            """
        ).fetchall()

        by_search = connection.execute(
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
                         AND JULIANDAY(COALESCE(sold_at, status_checked_at))
                             >= JULIANDAY(COALESCE(posted_at, first_seen))
                        THEN JULIANDAY(COALESCE(sold_at, status_checked_at))
                           - JULIANDAY(COALESCE(posted_at, first_seen))
                    END
                ) AS average_days_to_sell
            FROM listings
            GROUP BY search_id, search_name
            ORDER BY search_name
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
    average_days_to_sell = (
        sum(days_to_sell) / len(days_to_sell)
        if days_to_sell
        else None
    )

    return {
        "time": now_local,
        "total": total,
        "active": active,
        "sold": sold,
        "unknown": unknown,
        "new_today": int(new_today["count"] or 0),
        "sold_today": int(sold_today["count"] or 0),
        "sold_last_7_days": int(sold_last_7_days["count"] or 0),
        "sold_last_30_days": int(sold_last_30_days["count"] or 0),
        "average_last_asking_price": average_last_asking_price,
        "median_last_asking_price": _median_or_none(asking_prices),
        "average_sold_price": average_last_asking_price,
        "average_days_to_sell": average_days_to_sell,
        "median_days_to_sell": _median_or_none(days_to_sell),
        "sell_through_rate": sold / total * 100.0 if total else 0.0,
        "by_search": by_search,
    }


def _format_price(value: float | None) -> str:
    return "n/a" if value is None else f"£{float(value):.2f}"


def _format_days(value: float | None) -> str:
    return "n/a" if value is None else f"{float(value):.1f} days"


def build_message(stats: dict) -> str:
    """Build Telegram statistics message."""

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
        f"📅 Sold/unavailable last 7 days: {stats['sold_last_7_days']}",
        f"🗓 Sold/unavailable last 30 days: {stats['sold_last_30_days']}",
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
        lines.extend(
            [
                "",
                f"🏷 {row['search_name']}",
                f"  Active: {active} | Sold/unavailable: {sold}",
                f"  Sell-through: {rate:.1f}%",
                (
                    "  Avg asking: "
                    f"{_format_price(row['average_last_asking_price'])}"
                ),
                (
                    "  Avg time: "
                    f"{_format_days(row['average_days_to_sell'])}"
                ),
            ]
        )

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    """Send the statistics report to Telegram."""

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    if not CHAT_ID:
        raise RuntimeError("CHAT_ID is missing")

    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
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
    send_telegram(build_message(get_statistics()))
    LOGGER.info("Statistics sent successfully")


if __name__ == "__main__":
    main()
