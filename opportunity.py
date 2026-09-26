"""Market-value and resale-opportunity analysis for Vinted listings."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any, Final

from config import DATABASE_PATH
from models import Listing


MIN_COMPARABLES: Final[int] = 3
MAX_COMPARABLES: Final[int] = 200


@dataclass(frozen=True, slots=True)
class OpportunityAnalysis:
    """A conservative resale estimate derived from observed sold listings."""

    score: int | None
    label: str
    confidence: str
    estimated_resale: float | None
    gross_margin: float | None
    roi_percent: float | None
    comparable_count: int
    sell_through_rate: float | None
    average_days_to_sell: float | None
    scope: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Scope:
    label: str
    where_sql: str
    parameters: tuple[object, ...]


class OpportunityScorer:
    """Estimate market value from the agent's own sold-listing database."""

    def __init__(
        self,
        database_path: str | Path = DATABASE_PATH,
    ) -> None:
        self.database_path = Path(database_path)

    def analyse(
        self,
        listing: Listing,
    ) -> OpportunityAnalysis | None:
        """Return an opportunity analysis for a listing.

        Only rows explicitly classified as ``sold`` are used as valuation
        comparables. ``not_found`` rows are deliberately excluded because a
        missing Vinted page is not reliable proof of a completed sale.
        """

        search_id = str(listing.search_id or "").strip()

        if not search_id:
            return None

        connection = sqlite3.connect(
            str(self.database_path),
            timeout=5,
        )
        connection.row_factory = sqlite3.Row

        try:
            scope, sold_rows = self._select_scope(
                connection,
                listing,
                search_id,
            )

            comparable_count = len(sold_rows)

            if comparable_count == 0:
                return OpportunityAnalysis(
                    score=None,
                    label="building_data",
                    confidence="insufficient",
                    estimated_resale=None,
                    gross_margin=None,
                    roi_percent=None,
                    comparable_count=0,
                    sell_through_rate=None,
                    average_days_to_sell=None,
                    scope=scope.label,
                )

            prices = [
                float(row["current_price"])
                for row in sold_rows
                if float(row["current_price"] or 0) > 0
            ]

            if not prices:
                return OpportunityAnalysis(
                    score=None,
                    label="building_data",
                    confidence="insufficient",
                    estimated_resale=None,
                    gross_margin=None,
                    roi_percent=None,
                    comparable_count=0,
                    sell_through_rate=None,
                    average_days_to_sell=None,
                    scope=scope.label,
                )

            estimated_resale = round(float(median(prices)), 2)
            activity = self._activity_metrics(
                connection,
                listing.id,
                scope,
            )

            asking_price = max(0.0, float(listing.price_value))
            gross_margin = round(
                estimated_resale - asking_price,
                2,
            )

            roi_percent = (
                round(gross_margin / asking_price * 100.0, 1)
                if asking_price > 0
                else None
            )

            confidence = self._confidence(comparable_count)

            if comparable_count < MIN_COMPARABLES:
                return OpportunityAnalysis(
                    score=None,
                    label="building_data",
                    confidence=confidence,
                    estimated_resale=estimated_resale,
                    gross_margin=gross_margin,
                    roi_percent=roi_percent,
                    comparable_count=comparable_count,
                    sell_through_rate=activity["sell_through_rate"],
                    average_days_to_sell=activity["average_days_to_sell"],
                    scope=scope.label,
                )

            score = self._score(
                asking_price=asking_price,
                estimated_resale=estimated_resale,
                comparable_count=comparable_count,
                sell_through_rate=activity["sell_through_rate"],
                average_days_to_sell=activity["average_days_to_sell"],
            )

            return OpportunityAnalysis(
                score=score,
                label=self._label(score),
                confidence=confidence,
                estimated_resale=estimated_resale,
                gross_margin=gross_margin,
                roi_percent=roi_percent,
                comparable_count=comparable_count,
                sell_through_rate=activity["sell_through_rate"],
                average_days_to_sell=activity["average_days_to_sell"],
                scope=scope.label,
            )
        finally:
            connection.close()

    def _select_scope(
        self,
        connection: sqlite3.Connection,
        listing: Listing,
        search_id: str,
    ) -> tuple[_Scope, list[sqlite3.Row]]:
        scopes = self._candidate_scopes(
            listing,
            search_id,
        )

        fallback_scope = scopes[-1]
        fallback_rows: list[sqlite3.Row] = []

        for scope in scopes:
            rows = self._sold_rows(
                connection,
                listing.id,
                scope,
            )

            if scope is fallback_scope:
                fallback_rows = rows

            if len(rows) >= MIN_COMPARABLES:
                return scope, rows

        return fallback_scope, fallback_rows

    @staticmethod
    def _candidate_scopes(
        listing: Listing,
        search_id: str,
    ) -> list[_Scope]:
        brand = str(listing.brand or "").strip()
        size = str(listing.size or "").strip()
        condition = str(listing.condition or "").strip()

        scopes: list[_Scope] = []

        if brand and size and condition:
            scopes.append(
                _Scope(
                    label=f"brand+size+condition:{brand} / {size} / {condition}",
                    where_sql=(
                        "LOWER(TRIM(brand)) = LOWER(TRIM(?)) "
                        "AND LOWER(TRIM(size)) = LOWER(TRIM(?)) "
                        "AND LOWER(TRIM(item_condition)) = LOWER(TRIM(?))"
                    ),
                    parameters=(brand, size, condition),
                )
            )

        if brand and size:
            scopes.append(
                _Scope(
                    label=f"brand+size:{brand} / {size}",
                    where_sql=(
                        "LOWER(TRIM(brand)) = LOWER(TRIM(?)) "
                        "AND LOWER(TRIM(size)) = LOWER(TRIM(?))"
                    ),
                    parameters=(brand, size),
                )
            )

        if brand:
            scopes.append(
                _Scope(
                    label=f"brand:{brand}",
                    where_sql="LOWER(TRIM(brand)) = LOWER(TRIM(?))",
                    parameters=(brand,),
                )
            )

        if size:
            scopes.append(
                _Scope(
                    label=f"search+size:{search_id} / {size}",
                    where_sql=(
                        "search_id = ? "
                        "AND LOWER(TRIM(size)) = LOWER(TRIM(?))"
                    ),
                    parameters=(search_id, size),
                )
            )

        scopes.append(
            _Scope(
                label=f"search:{search_id}",
                where_sql="search_id = ?",
                parameters=(search_id,),
            )
        )

        return scopes

    @staticmethod
    def _sold_rows(
        connection: sqlite3.Connection,
        listing_id: str,
        scope: _Scope,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            f"""
            SELECT
                current_price,
                sold_at,
                COALESCE(posted_at, first_seen) AS market_started_at
            FROM listings
            WHERE listing_status = 'sold'
              AND current_price > 0
              AND id != ?
              AND {scope.where_sql}
            ORDER BY sold_at DESC, first_seen DESC
            LIMIT ?
            """,
            (
                listing_id,
                *scope.parameters,
                MAX_COMPARABLES,
            ),
        ).fetchall()

    @staticmethod
    def _activity_metrics(
        connection: sqlite3.Connection,
        listing_id: str,
        scope: _Scope,
    ) -> dict[str, float | None]:
        row = connection.execute(
            f"""
            SELECT
                SUM(
                    CASE
                        WHEN listing_status = 'active' THEN 1
                        ELSE 0
                    END
                ) AS active_count,
                SUM(
                    CASE
                        WHEN listing_status = 'sold' THEN 1
                        ELSE 0
                    END
                ) AS sold_count,
                AVG(
                    CASE
                        WHEN listing_status = 'sold'
                         AND sold_at IS NOT NULL
                        THEN
                            JULIANDAY(sold_at)
                            - JULIANDAY(COALESCE(posted_at, first_seen))
                    END
                ) AS average_days_to_sell
            FROM listings
            WHERE id != ?
              AND listing_status IN ('active', 'sold')
              AND {scope.where_sql}
            """,
            (
                listing_id,
                *scope.parameters,
            ),
        ).fetchone()

        active_count = int(row["active_count"] or 0)
        sold_count = int(row["sold_count"] or 0)
        total = active_count + sold_count

        sell_through_rate = (
            round(sold_count / total * 100.0, 1)
            if total
            else None
        )

        average_days = row["average_days_to_sell"]

        return {
            "sell_through_rate": sell_through_rate,
            "average_days_to_sell": (
                round(float(average_days), 1)
                if average_days is not None
                else None
            ),
        }

    @staticmethod
    def _score(
        *,
        asking_price: float,
        estimated_resale: float,
        comparable_count: int,
        sell_through_rate: float | None,
        average_days_to_sell: float | None,
    ) -> int:
        if estimated_resale <= 0:
            return 0

        discount_ratio = max(
            0.0,
            min(
                0.75,
                (estimated_resale - asking_price)
                / estimated_resale,
            ),
        )
        value_points = discount_ratio / 0.75 * 55.0

        sell_rate = max(
            0.0,
            min(100.0, sell_through_rate or 0.0),
        )
        sell_points = sell_rate / 100.0 * 20.0

        if average_days_to_sell is None:
            speed_points = 3.0
        elif average_days_to_sell <= 3:
            speed_points = 10.0
        elif average_days_to_sell <= 7:
            speed_points = 8.0
        elif average_days_to_sell <= 14:
            speed_points = 6.0
        elif average_days_to_sell <= 30:
            speed_points = 3.0
        else:
            speed_points = 1.0

        confidence_points = min(
            15.0,
            comparable_count / 20.0 * 15.0,
        )

        score = int(
            round(
                value_points
                + sell_points
                + speed_points
                + confidence_points
            )
        )

        if asking_price >= estimated_resale:
            score = min(score, 35)

        return max(0, min(100, score))

    @staticmethod
    def _confidence(comparable_count: int) -> str:
        if comparable_count >= 20:
            return "high"
        if comparable_count >= 8:
            return "medium"
        if comparable_count >= MIN_COMPARABLES:
            return "low"
        return "insufficient"

    @staticmethod
    def _label(score: int) -> str:
        if score >= 80:
            return "exceptional"
        if score >= 65:
            return "strong"
        if score >= 50:
            return "promising"
        return "normal"
