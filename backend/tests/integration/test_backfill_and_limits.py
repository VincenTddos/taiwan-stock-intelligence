"""Backfill resumability and central rate limiting."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from app.ingest import jobs
from app.models.market import DailyPrice, TradingCalendar
from app.models.ops import BackfillStatus, Transport
from app.providers.base import (
    ProviderError,
    ProviderErrorKind,
    ProviderResult,
    SourceMetadata,
)
from app.providers.rate_limiter import RateLimitConfig, RateLimiter, RateLimitExceeded
from app.services.backfill_service import BackfillService
from app.services.calendar_service import TradingCalendarService

pytestmark = pytest.mark.integration


@pytest.fixture
async def calendar_ready(session, registry, seeded_sources):
    await jobs.ingest_trading_calendar(session, registry, year=2026)
    await session.commit()


def _result(day: date, symbols: tuple[str, ...] = ("2330", "2454")) -> ProviderResult:
    """A minimal but structurally valid daily-price payload for one session."""
    return ProviderResult(
        records=[
            {
                "symbol": s,
                "market": "TWSE",
                "trading_date": day,
                "open": 100,
                "high": 105,
                "low": 99,
                "close": 104,
                "volume": 1000,
                "turnover": 104000,
                "source": "TWSE",
            }
            for s in symbols
        ],
        metadata=SourceMetadata(
            source="TWSE",
            source_endpoint="test://backfill",
            dataset="daily_prices",
            transport=Transport.REPLAY,
            source_request_at=datetime.now(UTC),
            data_as_of=day,
        ),
    )


# ============================================================== backfill
class TestBackfillResume:
    async def test_units_come_from_the_trading_calendar(self, session, calendar_ready):
        svc = BackfillService(session)
        cp = await svc.get_or_create(
            job_key="t:cal",
            dataset="daily_prices",
            source="TWSE",
            range_start=date(2026, 2, 9),
            range_end=date(2026, 2, 27),
        )
        units = await svc.plan_units(cp)

        # No weekends, and none of the Lunar New Year closure.
        assert all(u.weekday() < 5 for u in units)
        assert date(2026, 2, 16) not in units  # 春節
        assert date(2026, 2, 12) not in units  # settlement-only
        assert date(2026, 2, 11) in units  # 春節前最後交易日 — market open

    async def test_interrupted_backfill_resumes_from_checkpoint(self, session, calendar_ready):
        """The requirement: an interrupted run must not restart from the
        beginning. Ten years of restarts is a denial-of-service against the
        exchange, not a backfill."""
        svc = BackfillService(session)
        cp = await svc.get_or_create(
            job_key="t:resume",
            dataset="daily_prices",
            source="TWSE",
            range_start=date(2026, 7, 1),
            range_end=date(2026, 7, 31),
        )
        provider = object()
        calls: list[date] = []

        async def fetch(_p: object, day: date) -> ProviderResult:
            calls.append(day)
            return _result(day)

        # First run: stop after 5 sessions, as an interruption would.
        first = await svc.run(cp, provider, fetch=fetch, max_units=5)  # type: ignore[arg-type]
        assert first.units_done == 5
        assert cp.status == BackfillStatus.PAUSED
        cursor_after_first = cp.cursor
        first_calls = list(calls)

        # Second run resumes; it must not re-request anything already done.
        calls.clear()
        second = await svc.run(cp, provider, fetch=fetch)  # type: ignore[arg-type]

        assert not set(calls) & set(first_calls), "resumed run re-fetched completed units"
        assert min(calls) >= cursor_after_first
        assert second.units_done == len(first_calls) + len(calls)
        assert cp.status == BackfillStatus.COMPLETED

        # 23 weekdays in July 2026 with no published closure. Note the exchange's
        # own data for 2330 covers only 22 of them — see
        # test_calendar_discrepancy_is_detectable below.
        units = await svc.calendar.trading_days(date(2026, 7, 1), date(2026, 7, 31))
        total = (await session.execute(select(func.count()).select_from(DailyPrice))).scalar_one()
        assert total == len(units) * 2

    async def test_checkpoint_survives_a_new_service_instance(self, session, calendar_ready):
        """Progress lives in the database, not in process memory."""
        svc = BackfillService(session)
        cp = await svc.get_or_create(
            job_key="t:persist",
            dataset="daily_prices",
            source="TWSE",
            range_start=date(2026, 7, 1),
            range_end=date(2026, 7, 31),
        )

        async def fetch(_p: object, day: date) -> ProviderResult:
            return _result(day)

        await svc.run(cp, object(), fetch=fetch, max_units=3)  # type: ignore[arg-type]
        cursor = cp.cursor
        done = cp.units_done

        reloaded = await BackfillService(session).get_or_create(
            job_key="t:persist",
            dataset="daily_prices",
            source="TWSE",
            range_start=date(2026, 7, 1),
            range_end=date(2026, 7, 31),
        )
        assert reloaded.id == cp.id
        assert reloaded.cursor == cursor
        assert reloaded.units_done == done

    async def test_a_failing_unit_does_not_abort_the_run(self, session, calendar_ready):
        svc = BackfillService(session)
        cp = await svc.get_or_create(
            job_key="t:partial",
            dataset="daily_prices",
            source="TWSE",
            range_start=date(2026, 7, 1),
            range_end=date(2026, 7, 31),
        )
        bad_day = date(2026, 7, 8)

        async def fetch(_p: object, day: date) -> ProviderResult:
            if day == bad_day:
                raise ProviderError(
                    ProviderErrorKind.UPSTREAM_ERROR, "simulated 503", source="TWSE"
                )
            return _result(day)

        progress = await svc.run(cp, object(), fetch=fetch)  # type: ignore[arg-type]

        units = await svc.calendar.trading_days(date(2026, 7, 1), date(2026, 7, 31))
        assert progress.units_failed == 1
        assert progress.units_done == len(units) - 1
        assert bad_day.isoformat() in (cp.failed_units or [])
        assert cp.status == BackfillStatus.COMPLETED

    async def test_no_data_is_skipped_not_failed(self, session, calendar_ready):
        """A symbol that had not listed yet legitimately has no data."""
        svc = BackfillService(session)
        cp = await svc.get_or_create(
            job_key="t:nodata",
            dataset="daily_prices",
            source="TWSE",
            range_start=date(2026, 7, 1),
            range_end=date(2026, 7, 10),
        )

        async def fetch(_p: object, day: date) -> ProviderResult:
            raise ProviderError(ProviderErrorKind.NO_DATA, "not listed yet", source="TWSE")

        progress = await svc.run(cp, object(), fetch=fetch)  # type: ignore[arg-type]
        assert progress.units_failed == 0
        assert progress.units_skipped > 0

    async def test_systemic_failure_aborts_rather_than_hammering(self, session, calendar_ready):
        svc = BackfillService(session)
        cp = await svc.get_or_create(
            job_key="t:abort",
            dataset="daily_prices",
            source="TWSE",
            range_start=date(2026, 1, 5),
            range_end=date(2026, 7, 31),
        )

        async def fetch(_p: object, day: date) -> ProviderResult:
            raise ProviderError(ProviderErrorKind.UPSTREAM_ERROR, "source down", source="TWSE")

        await svc.run(cp, object(), fetch=fetch)  # type: ignore[arg-type]
        assert cp.status == BackfillStatus.FAILED
        assert cp.units_failed == 10, "should stop after MAX_CONSECUTIVE_FAILURES"
        assert "consecutive failures" in cp.last_error

    async def test_progress_rendering(self, session, calendar_ready):
        svc = BackfillService(session)
        cp = await svc.get_or_create(
            job_key="t:render",
            dataset="daily_prices",
            source="TWSE",
            range_start=date(2026, 7, 1),
            range_end=date(2026, 7, 31),
        )

        async def fetch(_p: object, day: date) -> ProviderResult:
            return _result(day)

        progress = await svc.run(cp, object(), fetch=fetch, max_units=11)  # type: ignore[arg-type]
        rendered = progress.render()
        assert "█" in rendered and "%" in rendered
        assert 45 <= progress.pct <= 55

    async def test_calendar_discrepancy_is_detectable(self, session, registry, calendar_ready):
        """A published schedule is a forecast; volume is evidence.

        The recorded TWSE data for 2330 covers 22 sessions in July 2026, but the
        published holiday schedule implies 23 — there is no exchange data for
        Friday 2026-07-10 and no holiday listed for it. Unannounced closures
        (typhoons) are exactly this shape: decided same-day, never present in the
        schedule published the previous December.

        `verify_against_observations` surfaces it instead of leaving a silent gap.
        """
        await jobs.ingest_daily_prices(session, registry, symbol="2330", month=date(2026, 7, 1))
        await session.commit()

        report = await TradingCalendarService(session).verify_against_observations(
            date(2026, 7, 1), date(2026, 7, 31)
        )
        await session.commit()

        assert "2026-07-10" in report["flagged_no_data"]
        assert int(report["confirmed"][0]) == 22

        # Days with observed trades are marked corroborated.
        row = await session.get(TradingCalendar, ("TWSE", date(2026, 7, 17)))
        assert row.verified_by_volume is True
        assert row.observed_symbol_count == 1


# ============================================================ rate limit
class TestRateLimiter:
    async def test_requests_are_throttled_to_the_configured_rate(self, redis_client):
        """Six requests at 3/min must not all pass immediately."""
        limiter = RateLimiter(redis_client)
        cfg = RateLimitConfig(source="RLTEST", requests_per_minute=3, max_concurrency=5)

        allowed = 0
        for _ in range(3):
            async with limiter.slot(cfg):
                allowed += 1
        assert allowed == 3

        # The fourth would have to wait for the window to roll.
        wait_ms = await limiter._try_minute(cfg)
        assert wait_ms > 0, "limiter should refuse an immediate 4th request"

    async def test_limit_is_shared_across_instances(self, redis_client):
        """Two workers must share one quota. A per-process limiter would let N
        workers each use the full rate, which is how an IP gets blocked."""
        cfg = RateLimitConfig(source="RLSHARED", requests_per_minute=2, max_concurrency=4)
        worker_a = RateLimiter(redis_client)
        worker_b = RateLimiter(redis_client)

        async with worker_a.slot(cfg):
            pass
        async with worker_b.slot(cfg):
            pass

        assert await worker_a._try_minute(cfg) > 0
        assert await worker_b._try_minute(cfg) > 0

    async def test_daily_cap_raises(self, redis_client):
        limiter = RateLimiter(redis_client)
        cfg = RateLimitConfig(
            source="RLDAY", requests_per_minute=100, requests_per_day=2, max_concurrency=2
        )
        for _ in range(2):
            async with limiter.slot(cfg):
                pass
        with pytest.raises(RateLimitExceeded, match="daily request limit"):
            async with limiter.slot(cfg):
                pass

    async def test_concurrency_is_capped(self, redis_client):
        limiter = RateLimiter(redis_client)
        cfg = RateLimitConfig(source="RLCONC", requests_per_minute=100, max_concurrency=2)
        peak = 0
        active = 0

        async def worker() -> None:
            nonlocal peak, active
            async with limiter.slot(cfg):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.05)
                active -= 1

        await asyncio.gather(*(worker() for _ in range(6)))
        assert peak <= 2, f"concurrency cap breached: {peak} in flight"

    async def test_minimum_interval_is_respected(self, redis_client):
        limiter = RateLimiter(redis_client)
        cfg = RateLimitConfig(
            source="RLGAP", requests_per_minute=1000, max_concurrency=1, min_interval_ms=120
        )
        started = time.monotonic()
        for _ in range(3):
            async with limiter.slot(cfg):
                pass
        # Two enforced gaps between three requests.
        assert time.monotonic() - started >= 0.20

    async def test_falls_back_to_local_limiting_without_redis(self):
        """Rate-limiting locally is strictly better than not at all."""
        limiter = RateLimiter(None)
        cfg = RateLimitConfig(source="RLLOCAL", requests_per_minute=2, max_concurrency=2)
        for _ in range(2):
            async with limiter.slot(cfg):
                pass
        assert await limiter._try_minute(cfg) > 0

    async def test_stats_report_usage(self, redis_client):
        limiter = RateLimiter(redis_client)
        cfg = RateLimitConfig(
            source="RLSTAT", requests_per_minute=10, requests_per_day=100, max_concurrency=2
        )
        async with limiter.slot(cfg):
            pass
        stats = await limiter.stats(cfg)
        assert stats["minute_used"] == 1
        assert stats["day_used"] == 1
        assert stats["requests_per_minute"] == 10
