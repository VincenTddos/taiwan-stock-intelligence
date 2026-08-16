"""Trading calendar.

The rule this service exists to enforce:

    A job's execution date is NEVER a trading date.

Snapshot endpoints have no date parameter. On a holiday they return the previous
session's data unchanged and with no indication that the market was closed. A
job that stamps `date.today()` on whatever it receives will, over a four-day
Lunar New Year closure, write the same session four times under four different
dates — and every one of those rows looks perfectly valid afterwards.

So the trading date always comes from the payload, and this service decides
whether that date is a day the market was actually open.

The calendar is built from two independent sources:

1. **The published schedule** (`/v1/holidaySchedule/holidaySchedule`) — a
   forecast. It also contains rows that are *annotations* rather than closures
   (`農曆春節前最後交易日` marks a day the market trades), so it cannot be
   consumed as "every row closes the market".
2. **Observed market activity** — evidence. A day with trades was open,
   whatever the schedule said. This corroboration is recorded in
   `verified_by_volume`.

When the two disagree, observation wins and the discrepancy is logged.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.market import DailyPrice, SessionType, TradingCalendar

log = get_logger(__name__)

WEEKEND = {5, 6}  # Saturday, Sunday


class CalendarNotPopulated(RuntimeError):
    """Raised when a job asks about a date the calendar does not cover.

    Deliberately fatal. Guessing would defeat the entire point of the guard.
    """

    def __init__(self, market: str, day: date) -> None:
        super().__init__(
            f"trading calendar for {market} has no entry for {day}. "
            f"Run the calendar sync before ingesting; ingestion must not guess."
        )


class TradingCalendarService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    async def is_trading_day(self, day: date, market: str = "TWSE") -> bool:
        row = await self.session.get(TradingCalendar, (market, day))
        if row is None:
            raise CalendarNotPopulated(market, day)
        return row.is_trading_day

    async def get(self, day: date, market: str = "TWSE") -> TradingCalendar | None:
        return await self.session.get(TradingCalendar, (market, day))

    async def assert_trading_day(self, day: date, market: str = "TWSE") -> None:
        """Guard for ingestion jobs. Raises if `day` was not a trading day."""
        row = await self.session.get(TradingCalendar, (market, day))
        if row is None:
            raise CalendarNotPopulated(market, day)
        if not row.is_trading_day:
            raise ValueError(
                f"{day} is not a trading day for {market} "
                f"({row.session_type}: {row.holiday_name or 'weekend'}). "
                f"Refusing to write market data for a closed session."
            )

    async def trading_days(self, start: date, end: date, market: str = "TWSE") -> list[date]:
        stmt = (
            select(TradingCalendar.calendar_date)
            .where(
                TradingCalendar.market == market,
                TradingCalendar.calendar_date >= start,
                TradingCalendar.calendar_date <= end,
                TradingCalendar.is_trading_day.is_(True),
            )
            .order_by(TradingCalendar.calendar_date)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def previous_trading_day(self, day: date, market: str = "TWSE") -> date | None:
        stmt = (
            select(TradingCalendar.calendar_date)
            .where(
                TradingCalendar.market == market,
                TradingCalendar.calendar_date < day,
                TradingCalendar.is_trading_day.is_(True),
            )
            .order_by(TradingCalendar.calendar_date.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def next_trading_day(self, day: date, market: str = "TWSE") -> date | None:
        stmt = (
            select(TradingCalendar.calendar_date)
            .where(
                TradingCalendar.market == market,
                TradingCalendar.calendar_date > day,
                TradingCalendar.is_trading_day.is_(True),
            )
            .order_by(TradingCalendar.calendar_date)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ------------------------------------------------------------------
    async def build_year(
        self,
        year: int,
        closures: list[dict[str, object]],
        market: str = "TWSE",
        source: str = "TWSE",
    ) -> dict[str, int]:
        """Materialise a full calendar year from the published closure list.

        Every day of the year gets a row. Weekends are closed; listed closures
        are closed; everything else is a trading day. Writing all 365 days —
        rather than only the exceptions — means `is_trading_day` can be answered
        by a primary-key lookup, and a missing row unambiguously means "the
        calendar was never built for this period" rather than "probably fine".
        """
        by_date: dict[date, dict[str, object]] = {}
        for rec in closures:
            day = rec["calendar_date"]
            assert isinstance(day, date)
            if day.year != year:
                continue
            by_date[day] = rec

        rows: list[dict[str, object]] = []
        cursor = date(year, 1, 1)
        end = date(year, 12, 31)
        trading = weekend = holiday = 0

        while cursor <= end:
            closure = by_date.get(cursor)
            is_weekend = cursor.weekday() in WEEKEND

            if closure is not None:
                session_type = str(closure.get("session_type") or SessionType.CLOSED)
                rows.append(
                    {
                        "market": market,
                        "calendar_date": cursor,
                        "is_trading_day": False,
                        "session_type": session_type,
                        "holiday_name": closure.get("holiday_name"),
                        "description": closure.get("description"),
                        "source": source,
                    }
                )
                holiday += 1
            elif is_weekend:
                rows.append(
                    {
                        "market": market,
                        "calendar_date": cursor,
                        "is_trading_day": False,
                        "session_type": str(SessionType.CLOSED),
                        "holiday_name": "週六" if cursor.weekday() == 5 else "週日",
                        "description": None,
                        "source": source,
                    }
                )
                weekend += 1
            else:
                rows.append(
                    {
                        "market": market,
                        "calendar_date": cursor,
                        "is_trading_day": True,
                        "session_type": str(SessionType.FULL),
                        "holiday_name": None,
                        "description": None,
                        "source": source,
                    }
                )
                trading += 1
            cursor += timedelta(days=1)

        await self.upsert(rows)
        log.info(
            "trading_calendar_built",
            market=market,
            year=year,
            trading_days=trading,
            weekend_days=weekend,
            holiday_days=holiday,
        )
        return {"trading": trading, "weekend": weekend, "holiday": holiday, "total": len(rows)}

    async def upsert(self, rows: list[dict[str, object]]) -> int:
        """Idempotent write. Re-running a calendar sync must not duplicate days."""
        if not rows:
            return 0
        stmt = pg_insert(TradingCalendar).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[TradingCalendar.market, TradingCalendar.calendar_date],
            set_={
                "is_trading_day": stmt.excluded.is_trading_day,
                "session_type": stmt.excluded.session_type,
                "holiday_name": stmt.excluded.holiday_name,
                "description": stmt.excluded.description,
                "source": stmt.excluded.source,
            },
        )
        await self.session.execute(stmt)
        return len(rows)

    # ------------------------------------------------------------------
    async def verify_against_observations(
        self, start: date, end: date, market: str = "TWSE", min_symbols: int = 1
    ) -> dict[str, list[str]]:
        """Corroborate the published calendar with observed trading activity.

        A published schedule is a forecast; volume is evidence. Typhoon closures
        in particular are announced same-day and never appear in the schedule
        published the previous December — this is how they get corrected.
        """
        stmt = (
            select(DailyPrice.trading_date, func.count(func.distinct(DailyPrice.symbol)))
            .where(
                DailyPrice.market == market,
                DailyPrice.trading_date >= start,
                DailyPrice.trading_date <= end,
            )
            .group_by(DailyPrice.trading_date)
        )
        observed = {d: int(n) for d, n in (await self.session.execute(stmt)).all()}

        cal_stmt = select(TradingCalendar).where(
            TradingCalendar.market == market,
            TradingCalendar.calendar_date >= start,
            TradingCalendar.calendar_date <= end,
        )
        calendar = {c.calendar_date: c for c in (await self.session.execute(cal_stmt)).scalars()}

        corrected_open: list[str] = []
        flagged_closed: list[str] = []
        confirmed = 0

        for day, row in calendar.items():
            count = observed.get(day, 0)
            if count >= min_symbols:
                if not row.is_trading_day:
                    # Evidence beats the schedule.
                    row.is_trading_day = True
                    row.session_type = str(SessionType.FULL)
                    row.description = (
                        f"reopened by observation ({count} symbols traded); "
                        f"schedule said: {row.holiday_name}"
                    )
                    corrected_open.append(day.isoformat())
                row.verified_by_volume = True
                row.observed_symbol_count = count
                confirmed += 1
            elif row.is_trading_day and day <= end:
                # No data for a day the schedule says was open. Could be an
                # unannounced closure (typhoon) or simply a gap in ingestion, so
                # it is flagged for review rather than silently closed.
                flagged_closed.append(day.isoformat())

        await self.session.flush()
        log.info(
            "trading_calendar_verified",
            market=market,
            confirmed=confirmed,
            corrected_open=len(corrected_open),
            flagged=len(flagged_closed),
        )
        return {
            "corrected_to_open": corrected_open,
            "flagged_no_data": flagged_closed,
            "confirmed": [str(confirmed)],
        }

    async def coverage(self, market: str = "TWSE") -> dict[str, object]:
        stmt = select(
            func.min(TradingCalendar.calendar_date),
            func.max(TradingCalendar.calendar_date),
            func.count(),
            func.count().filter(TradingCalendar.is_trading_day.is_(True)),
            func.count().filter(TradingCalendar.verified_by_volume.is_(True)),
        ).where(TradingCalendar.market == market)
        first, last, total, trading, verified = (await self.session.execute(stmt)).one()
        return {
            "market": market,
            "first_date": first,
            "last_date": last,
            "days": int(total or 0),
            "trading_days": int(trading or 0),
            "verified_by_volume": int(verified or 0),
        }


__all__ = ["WEEKEND", "CalendarNotPopulated", "TradingCalendarService"]
