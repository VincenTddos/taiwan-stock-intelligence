"""Data availability — what did we know, and when?

This answers the question every historical simulation has to ask and almost
every one gets wrong:

    At `as_of`, which records were actually knowable?

Two different clocks are involved and conflating them is the classic
look-ahead bug:

* **event time** — when the thing happened (`trading_date`, `period_end`,
  `ex_date`)
* **knowledge time** — when it became public (`announced_at`) or, failing that,
  when we received it (`ingested_at`)

A quarterly report for `period_end = 2026-03-31` disclosed on `2026-05-20` was
not available on `2026-05-15`, no matter what its period says. Filtering on
event time silently hands a backtest information from the future, and the
resulting performance looks excellent.

Every method here filters on knowledge time. Phase 6's backtest engine consumes
this service rather than querying tables directly, so the rule is enforced in one
place instead of at every call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.market import CorporateAction, DailyPrice, StockMaster

log = get_logger(__name__)


def _as_datetime(as_of: date | datetime) -> datetime:
    """Normalise to an inclusive end-of-day instant when given a date."""
    if isinstance(as_of, datetime):
        return as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    return datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, tzinfo=UTC)


@dataclass(slots=True)
class AvailabilityWindow:
    """What a dataset offered at a point in time."""

    dataset: str
    as_of: datetime
    available: bool
    latest_event_date: date | None = None
    record_count: int = 0
    detail: dict[str, Any] | None = None


class DataAvailabilityService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    def daily_price_filter(self, as_of: date | datetime) -> Any:
        """SQLAlchemy predicate for point-in-time price queries.

        Filters on `trading_date` alone, deliberately.

        For a price bar, event time and knowledge time coincide for our purposes:
        the market knew Friday's close at Friday's close. `ingested_at` records
        when *we* downloaded it, which is a completely different thing — a ten
        year backfill run today gives every historical bar an `ingested_at` of
        today. Including it in this filter would make every point-in-time query
        against backfilled history return nothing, which is how a correct-looking
        guard turns into a silently empty backtest.

        Fundamentals are the opposite case and are handled by
        `corporate_action_filter` / `announced_at`, where the two clocks genuinely
        differ by weeks.
        """
        return DailyPrice.trading_date <= _as_datetime(as_of).date()

    def corporate_action_filter(self, as_of: date | datetime) -> Any:
        """Only actions **announced** by `as_of`.

        Adjusting a historical price series with an action that had not been
        announced yet is look-ahead bias wearing a very convincing disguise: the
        adjusted series looks smooth and correct.
        """
        return CorporateAction.announced_at <= _as_datetime(as_of)

    def stock_master_filter(self, as_of: date | datetime) -> Any:
        """The SCD2 row that was current at `as_of`."""
        day = _as_datetime(as_of).date()
        return and_(
            StockMaster.valid_from <= day,
            or_(StockMaster.valid_to.is_(None), StockMaster.valid_to > day),
        )

    # ------------------------------------------------------------------
    async def available_at(
        self,
        dataset: str,
        as_of: date | datetime,
        *,
        symbol: str | None = None,
        market: str = "TWSE",
    ) -> AvailabilityWindow:
        cutoff = _as_datetime(as_of)

        if dataset == "daily_prices":
            stmt: Select[Any] = select(func.count(), func.max(DailyPrice.trading_date)).where(
                self.daily_price_filter(cutoff), DailyPrice.market == market
            )
            if symbol:
                stmt = stmt.where(DailyPrice.symbol == symbol)
            count, latest = (await self.session.execute(stmt)).one()

        elif dataset == "corporate_actions":
            stmt = select(func.count(), func.max(CorporateAction.ex_date)).where(
                self.corporate_action_filter(cutoff), CorporateAction.market == market
            )
            if symbol:
                stmt = stmt.where(CorporateAction.symbol == symbol)
            count, latest = (await self.session.execute(stmt)).one()

        elif dataset == "stock_master":
            stmt = select(func.count(), func.max(StockMaster.valid_from)).where(
                self.stock_master_filter(cutoff), StockMaster.market == market
            )
            if symbol:
                stmt = stmt.where(StockMaster.symbol == symbol)
            count, latest = (await self.session.execute(stmt)).one()

        else:
            raise ValueError(f"unknown dataset '{dataset}'")

        return AvailabilityWindow(
            dataset=dataset,
            as_of=cutoff,
            available=bool(count),
            latest_event_date=latest,
            record_count=int(count or 0),
            detail={"symbol": symbol, "market": market},
        )

    async def prices_as_of(
        self,
        as_of: date | datetime,
        *,
        symbols: list[str] | None = None,
        start: date | None = None,
        market: str = "TWSE",
        include_suspect: bool = False,
    ) -> list[DailyPrice]:
        """Price history exactly as it would have looked at `as_of`."""
        stmt = (
            select(DailyPrice)
            .where(self.daily_price_filter(as_of), DailyPrice.market == market)
            .order_by(DailyPrice.symbol, DailyPrice.trading_date)
        )
        if symbols:
            stmt = stmt.where(DailyPrice.symbol.in_(symbols))
        if start:
            stmt = stmt.where(DailyPrice.trading_date >= start)
        if not include_suspect:
            stmt = stmt.where(DailyPrice.quality_status == "OK")
        return list((await self.session.execute(stmt)).scalars().all())

    async def corporate_actions_as_of(
        self, as_of: date | datetime, *, symbol: str, market: str = "TWSE"
    ) -> list[CorporateAction]:
        stmt = (
            select(CorporateAction)
            .where(
                self.corporate_action_filter(as_of),
                CorporateAction.symbol == symbol,
                CorporateAction.market == market,
            )
            .order_by(CorporateAction.ex_date)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def universe_as_of(
        self, as_of: date | datetime, *, market: str = "TWSE", include_delisted: bool = True
    ) -> list[str]:
        """The symbols that existed at `as_of`.

        `include_delisted=True` is the default on purpose. A universe built from
        today's listed companies is survivorship bias: it silently excludes every
        company that failed, which is precisely the population a strategy needed
        to avoid.
        """
        day = _as_datetime(as_of).date()
        stmt = select(StockMaster.symbol).where(
            self.stock_master_filter(as_of),
            StockMaster.market == market,
            or_(StockMaster.listing_date.is_(None), StockMaster.listing_date <= day),
        )
        if not include_delisted:
            stmt = stmt.where(
                or_(StockMaster.delisting_date.is_(None), StockMaster.delisting_date > day)
            )
        return sorted(set((await self.session.execute(stmt)).scalars().all()))

    async def coverage_report(self, market: str = "TWSE") -> dict[str, Any]:
        stmt = select(
            func.count(),
            func.count(func.distinct(DailyPrice.symbol)),
            func.min(DailyPrice.trading_date),
            func.max(DailyPrice.trading_date),
            func.count().filter(DailyPrice.quality_status == "SUSPECT"),
        ).where(DailyPrice.market == market)
        rows, symbols, first, last, suspect = (await self.session.execute(stmt)).one()
        return {
            "market": market,
            "price_rows": int(rows or 0),
            "symbols": int(symbols or 0),
            "first_trading_date": first,
            "last_trading_date": last,
            "suspect_rows": int(suspect or 0),
        }


__all__ = ["AvailabilityWindow", "DataAvailabilityService"]
