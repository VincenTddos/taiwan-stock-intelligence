"""Data freshness.

Freshness is measured against the **trading calendar**, not the wall clock. A
daily price dataset whose newest row is Friday's close is perfectly fresh on
Sunday afternoon, and stale by Tuesday morning. Comparing `now - last_ingested`
to a fixed threshold would raise an alarm every weekend and every Lunar New
Year, which trains everyone to ignore it.

Four states, each meaning something different and actionable:

| Status   | Meaning | What to do |
|----------|---------|-----------|
| FRESH    | Up to date for the most recent completed session | nothing |
| STALE    | We have data, but it is older than expected | check the job |
| MISSING  | The dataset has never been populated | run the backfill |
| DEGRADED | Present and current, but partial or with quality problems | check quarantine |

Stale data is still **served**, clearly labelled. Hiding it would leave the user
staring at an empty screen with no idea why.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.market import DailyPrice, IndexQuote, InstitutionalFlow, StockMaster
from app.models.ops import DataFreshness, DataQuarantine, FreshnessStatus
from app.services.calendar_service import TradingCalendarService

log = get_logger(__name__)

# Taipei close is 13:30 local = 05:30 UTC. Lags are measured from there.
TAIPEI_CLOSE_UTC_HOUR = 5
TAIPEI_CLOSE_UTC_MINUTE = 30

# dataset -> (model, date column, expected minutes after close, description)
TRACKED: dict[str, tuple[Any, Any, int, str]] = {
    "daily_prices": (DailyPrice, DailyPrice.trading_date, 90, "個股日成交資訊"),
    "index_quotes": (IndexQuote, IndexQuote.trading_date, 60, "指數收盤行情"),
    "institutional_flow": (
        InstitutionalFlow,
        InstitutionalFlow.trading_date,
        180,
        "三大法人買賣超",
    ),
    "stock_master": (StockMaster, StockMaster.valid_from, 1440, "上市公司基本資料"),
}


class DataFreshnessService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.calendar = TradingCalendarService(session)

    # ------------------------------------------------------------------
    async def _expected_data_date(self, now: datetime, market: str = "TWSE") -> date | None:
        """The most recent session whose data we should already have.

        If today is a trading day and the close has passed, today counts.
        Otherwise the previous trading day does.
        """
        today = now.astimezone(UTC).date()
        close_today = datetime(
            today.year,
            today.month,
            today.day,
            TAIPEI_CLOSE_UTC_HOUR,
            TAIPEI_CLOSE_UTC_MINUTE,
            tzinfo=UTC,
        )
        cal = await self.calendar.get(today, market)
        if cal is not None and cal.is_trading_day and now >= close_today:
            return today
        return await self.calendar.previous_trading_day(today, market)

    async def evaluate(
        self, dataset: str, *, market: str = "TWSE", now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        model, date_col, expected_lag, description = TRACKED[dataset]

        stmt = select(func.max(date_col), func.count(), func.max(model.ingested_at))
        if hasattr(model, "market"):
            stmt = stmt.where(model.market == market)
        last_data_date, record_count, last_ingested = (await self.session.execute(stmt)).one()

        quarantined = (
            await self.session.execute(
                select(func.count())
                .select_from(DataQuarantine)
                .where(
                    DataQuarantine.dataset == dataset,
                    DataQuarantine.reviewed_at.is_(None),
                )
            )
        ).scalar_one()

        expected_date = await self._expected_data_date(now, market)

        status = FreshnessStatus.FRESH
        lag_minutes: int | None = None
        detail: dict[str, Any] = {
            "expected_data_date": expected_date.isoformat() if expected_date else None,
            "unreviewed_quarantine": int(quarantined or 0),
        }

        if not record_count or last_data_date is None:
            status = FreshnessStatus.MISSING
            detail["reason"] = "dataset has never been populated"
        elif expected_date is None:
            status = FreshnessStatus.DEGRADED
            detail["reason"] = "trading calendar not populated; cannot judge freshness"
        elif last_data_date >= expected_date:
            status = FreshnessStatus.FRESH
            lag_minutes = 0
        else:
            # How many minutes past the point the data should have arrived.
            due = datetime(
                expected_date.year,
                expected_date.month,
                expected_date.day,
                TAIPEI_CLOSE_UTC_HOUR,
                TAIPEI_CLOSE_UTC_MINUTE,
                tzinfo=UTC,
            ) + timedelta(minutes=expected_lag)
            lag_minutes = max(0, int((now - due).total_seconds() // 60))
            status = FreshnessStatus.STALE if now > due else FreshnessStatus.FRESH
            detail["reason"] = (
                f"newest row is {last_data_date}, expected {expected_date}"
                if status is FreshnessStatus.STALE
                else "within the expected publication window"
            )

        if status is FreshnessStatus.FRESH and quarantined:
            status = FreshnessStatus.DEGRADED
            detail["reason"] = f"{quarantined} record(s) awaiting quarantine review"

        expected_next = None
        if expected_date is not None:
            nxt = await self.calendar.next_trading_day(expected_date, market)
            if nxt:
                expected_next = datetime(
                    nxt.year,
                    nxt.month,
                    nxt.day,
                    TAIPEI_CLOSE_UTC_HOUR,
                    TAIPEI_CLOSE_UTC_MINUTE,
                    tzinfo=UTC,
                ) + timedelta(minutes=expected_lag)

        payload = {
            "dataset": dataset,
            "market": market,
            "description": description,
            "expected_lag_minutes": expected_lag,
            "expected_frequency": "TRADING_DAY",
            "last_data_date": last_data_date,
            "last_ingested_at": last_ingested,
            "last_success_at": last_ingested,
            "expected_next_update": expected_next,
            "record_count": int(record_count or 0),
            "status": str(status),
            "lag_minutes": lag_minutes,
            "detail": detail,
            "checked_at": now,
        }

        stmt_up = pg_insert(DataFreshness).values(payload)
        await self.session.execute(
            stmt_up.on_conflict_do_update(
                index_elements=[DataFreshness.dataset],
                set_={k: getattr(stmt_up.excluded, k) for k in payload if k not in ("dataset",)},
            )
        )
        return payload

    async def evaluate_all(
        self, *, market: str = "TWSE", now: datetime | None = None
    ) -> list[dict[str, Any]]:
        results = [await self.evaluate(ds, market=market, now=now) for ds in TRACKED]
        await self.session.flush()
        log.info(
            "freshness_evaluated",
            datasets=len(results),
            statuses={r["dataset"]: r["status"] for r in results},
        )
        return results

    async def current(self) -> list[DataFreshness]:
        stmt = select(DataFreshness).order_by(DataFreshness.dataset)
        return list((await self.session.execute(stmt)).scalars().all())

    @staticmethod
    def overall(rows: list[dict[str, Any]] | list[DataFreshness]) -> str:
        """Worst status across datasets — what the dashboard header shows."""
        order = [
            FreshnessStatus.MISSING,
            FreshnessStatus.STALE,
            FreshnessStatus.DEGRADED,
            FreshnessStatus.FRESH,
        ]
        statuses = {str(r["status"] if isinstance(r, dict) else r.status) for r in rows}
        for level in order:
            if str(level) in statuses:
                return str(level)
        return str(FreshnessStatus.MISSING)


__all__ = ["TRACKED", "DataFreshnessService"]
