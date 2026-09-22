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
FASTEST_LIMIT = 5
TOP_SIZE_LIMIT = 8


def utc_timestamp(value: datetime) -> str:
    """Convert an aware datetime to SQLite UTC timestamp format."""

    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _median_or_none(values: list[float]) -> float | None:
    """Return a median value, or None for an empty list."""

    return float(median(values)) if values else None


def _format_price(value: float | None) -> str:
    """Format a price for Telegram."""

    return "n/a" if value is None else f"£{float(value):.2f}"


def _format_days(value: float | None) -> str:
    """Format a duration for Telegram."""

    return "n/a" if value is None else f"{float(value):.1f} days"


def _sell_through(sold: int, total: int) -> float:
    """Return sold/unavailable as a percentage of all tracked rows."""

    return sold / total * 100.0 if total else 0.0


def _short_title(value: str, limit: int = 48) -> str:
    """Keep fastest-sale rows compact enough for Telegram."""

    cleaned = " ".join((value or "Untitled").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


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
                SUM(CASE WHEN listing_status IN ('sold', 'not_found') THEN 1 ELSE 0 END) AS sold,
                SUM(CASE WHEN listing_status = 'unknown' THEN 1 ELSE 0 END) AS unknown,
                SUM(CASE WHEN size IS NOT NULL AND TRIM(size) <> '' THEN 1 ELSE 0 END) AS size_enriched,
                SUM(CASE WHEN item_condition IS NOT NULL AND TRIM(item_condition) <> '' THEN 1 ELSE 0 END) AS condition_enriched,
                SUM(CASE WHEN posted_at IS NOT NULL AND TRIM(posted_at) <> '' THEN 1 ELSE 0 END) AS posted_at_enriched
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
                title,
                search_name,
                current_price,
                url,
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
                SUM(CASE WHEN listing_status IN ('sold', 'not_found') THEN 1 ELSE 0 END) AS sold,
                SUM(CASE WHEN listing_status = 'unknown' THEN 1 ELSE 0 END) AS unknown,
                AVG(CASE WHEN listing_status IN ('sold', 'not_found') THEN current_price END) AS average_last_asking_price,
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

        by_condition = connection.execute(
            """
            SELECT
                item_condition AS label,
                COUNT(*) AS total,
                SUM(CASE WHEN listing_status = 'active' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN listing_status IN ('sold', 'not_found') THEN 1 ELSE 0 END) AS sold,
                SUM(CASE WHEN listing_status = 'unknown' THEN 1 ELSE 0 END) AS unknown
            FROM listings
            WHERE item_condition IS NOT NULL
              AND TRIM(item_condition) <> ''
            GROUP BY item_condition
            ORDER BY total DESC, item_condition
            """
        ).fetchall()

        by_size = connection.execute(
            """
            SELECT
                size AS label,
                COUNT(*) AS total,
                SUM(CASE WHEN listing_status = 'active' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN listing_status IN ('sold', 'not_found') THEN 1 ELSE 0 END) AS sold,
                SUM(CASE WHEN listing_status = 'unknown' THEN 1 ELSE 0 END) AS unknown
            FROM listings
            WHERE size IS NOT NULL
              AND TRIM(size) <> ''
            GROUP BY size
            ORDER BY total DESC, size
            LIMIT ?
            """,
            (TOP_SIZE_LIMIT,),
        ).fetchall()

        price_bands = connection.execute(
            """
            SELECT
                CASE
                    WHEN current_price <= 20 THEN '£0-20'
                    WHEN current_price <= 40 THEN '£20-40'
                    WHEN current_price <= 60 THEN '£40-60'
                    ELSE '£60+'
                END AS label,
                COUNT(*) AS total,
                SUM(CASE WHEN listing_status = 'active' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN listing_status IN ('sold', 'not_found') THEN 1 ELSE 0 END) AS sold,
                SUM(CASE WHEN listing_status = 'unknown' THEN 1 ELSE 0 END) AS unknown
            FROM listings
            WHERE current_price > 0
            GROUP BY label
            ORDER BY
                CASE label
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
        if row["current_price"] is not None and float(row["current_price"]) > 0
    ]
    days_to_sell = [
        float(row["days_to_sell"])
        for row in sold_rows
        if row["days_to_sell"] is not None and float(row["days_to_sell"]) >= 0
    ]

    average_last_asking_price = (
        sum(asking_prices) / len(asking_prices) if asking_prices else None
    )
    average_days_to_sell = (
        sum(days_to_sell) / len(days_to_sell) if days_to_sell else None
    )

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
            if row["days_to_sell"] is not None and float(row["days_to_sell"]) >= 0
        ),
        key=lambda item: item["days"],
    )[:FASTEST_LIMIT]

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
        "sell_through_rate": _sell_through(sold, total),
        "size_enriched": int(totals["size_enriched"] or 0),
        "condition_enriched": int(totals["condition_enriched"] or 0),
        "posted_at_enriched": int(totals["posted_at_enriched"] or 0),
        "by_search": [dict(row) for row in by_search],
        "by_condition": [dict(row) for row in by_condition],
        "by_size": [dict(row) for row in by_size],
        "price_bands": [dict(row) for row in price_bands],
        "fastest_sales": fastest_sales,
    }


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
        "💷 Average last asking price: " + _format_price(stats["average_last_asking_price"]),
        "💷 Median last asking price: " + _format_price(stats["median_last_asking_price"]),
        "⏱ Average time to sell/unavailable: " + _format_days(stats["average_days_to_sell"]),
        "⏱ Median time to sell/unavailable: " + _format_days(stats["median_days_to_sell"]),
        "",
        (
            "🧩 Enrichment coverage: "
            f"size {stats['size_enriched']}/{stats['total']} | "
            f"condition {stats['condition_enriched']}/{stats['total']} | "
            f"posted {stats['posted_at_enriched']}/{stats['total']}"
        ),
        "   Time-to-sell uses Vinted posted time when available; first seen otherwise.",
        "",
        "🔎 BY SEARCH",
    ]

    for row in stats["by_search"]:
        total = int(row["total"] or 0)
        active = int(row["active"] or 0)
        sold = int(row["sold"] or 0)
        unknown = int(row["unknown"] or 0)
        lines.extend(
            [
                "",
                f"🏷 {row['search_name']}",
                f"  Active: {active} | Sold/unavailable: {sold} | Unknown: {unknown}",
                f"  Sell-through: {_sell_through(sold, total):.1f}%",
                "  Avg asking: " + _format_price(row["average_last_asking_price"]),
                "  Avg time: " + _format_days(row["average_days_to_sell"]),
            ]
        )

    if stats["price_bands"]:
        lines.extend(["", "💷 PRICE BANDS"])
        for row in stats["price_bands"]:
            total = int(row["total"] or 0)
            sold = int(row["sold"] or 0)
            active = int(row["active"] or 0)
            lines.append(
                f"  {row['label']}: {active} active | {sold}/{total} sold/unavailable "
                f"({_sell_through(sold, total):.1f}%)"
            )

    if stats["by_condition"]:
        lines.extend(["", "🧾 BY CONDITION"])
        for row in stats["by_condition"]:
            total = int(row["total"] or 0)
            sold = int(row["sold"] or 0)
            lines.append(
                f"  {row['label']}: {sold}/{total} sold/unavailable "
                f"({_sell_through(sold, total):.1f}%)"
            )

    if stats["by_size"]:
        lines.extend(["", "📏 TOP SIZES BY SAMPLE"])
        for row in stats["by_size"]:
            total = int(row["total"] or 0)
            sold = int(row["sold"] or 0)
            lines.append(
                f"  {row['label']}: {sold}/{total} sold/unavailable "
                f"({_sell_through(sold, total):.1f}%)"
            )

    if stats["fastest_sales"]:
        lines.extend(["", "⚡ FASTEST SOLD/UNAVAILABLE"])
        for item in stats["fastest_sales"]:
            lines.append(
                f"  {_short_title(item['title'])} | {item['days']:.1f}d | "
                f"{_format_price(item['price'])} | {item['search_name']}"
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
