"""The Phase 2 guarantees, each asserted against real recorded data.

Every test here corresponds to a named requirement:

* holiday ingestion must never create a fake trading day
* the same (source, symbol, date) must be idempotent
* invalid OHLC goes to quarantine, never silently dropped
* every accepted record has source and timestamps
* raw prices stay immutable; adjusted prices are derived separately
* `available_at` answers what was knowable at a past instant
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.ingest import jobs
from app.ingest.pipeline import IngestionPipeline
from app.models.market import (
    CorporateAction,
    DailyPrice,
    IndexQuote,
    InstitutionalFlow,
    StockMaster,
    TradingCalendar,
)
from app.models.ops import DataQuarantine, RawIngestion, Transport
from app.providers.base import ProviderResult, SourceMetadata
from app.services.availability_service import DataAvailabilityService
from app.services.calendar_service import CalendarNotPopulated, TradingCalendarService

pytestmark = pytest.mark.integration

SNAPSHOT_DATE = date(2026, 8, 14)
HISTORY_MONTH = date(2026, 7, 1)


@pytest.fixture
async def calendar_ready(session, registry, seeded_sources):
    await jobs.ingest_trading_calendar(session, registry, year=2026)
    await session.commit()


# ============================================================== calendar
class TestTradingCalendar:
    async def test_year_is_fully_materialised(self, session, calendar_ready):
        total = (
            await session.execute(select(func.count()).select_from(TradingCalendar))
        ).scalar_one()
        assert total == 365

    async def test_weekends_are_not_trading_days(self, session, calendar_ready):
        svc = TradingCalendarService(session)
        assert await svc.is_trading_day(date(2026, 8, 15)) is False  # Saturday
        assert await svc.is_trading_day(date(2026, 8, 16)) is False  # Sunday
        assert await svc.is_trading_day(date(2026, 8, 14)) is True  # Friday

    async def test_published_holidays_close_the_market(self, session, calendar_ready):
        svc = TradingCalendarService(session)
        for closed in (
            date(2026, 1, 1),  # 開國紀念日
            date(2026, 2, 16),  # 春節
            date(2026, 5, 1),  # 勞動節
            date(2026, 10, 9),  # 國慶日補假
        ):
            assert await svc.is_trading_day(closed) is False, closed

    async def test_annotation_rows_remain_trading_days(self, session, calendar_ready):
        """`農曆春節前最後交易日` and `國曆新年開始交易日` appear in the holiday
        feed but the market is open. Treating them as closures would delete
        three real sessions."""
        svc = TradingCalendarService(session)
        assert await svc.is_trading_day(date(2026, 2, 11)) is True
        assert await svc.is_trading_day(date(2026, 1, 2)) is True
        assert await svc.is_trading_day(date(2026, 2, 23)) is True

    async def test_settlement_only_day_is_closed_for_trading(self, session, calendar_ready):
        row = await session.get(TradingCalendar, ("TWSE", date(2026, 2, 12)))
        assert row.is_trading_day is False
        assert row.session_type == "SETTLEMENT_ONLY"

    async def test_missing_calendar_is_fatal_not_guessed(self, session, seeded_sources):
        with pytest.raises(CalendarNotPopulated):
            await TradingCalendarService(session).is_trading_day(date(2019, 5, 2))

    async def test_assert_trading_day_guard(self, session, calendar_ready):
        svc = TradingCalendarService(session)
        await svc.assert_trading_day(date(2026, 8, 14))
        with pytest.raises(ValueError, match="not a trading day"):
            await svc.assert_trading_day(date(2026, 1, 1))


# ====================================================== holiday ingestion
class TestHolidayIngestion:
    async def test_holiday_data_never_creates_a_trading_day(
        self, session, registry, calendar_ready
    ):
        """The scenario this whole design exists to prevent.

        A snapshot endpoint has no date parameter. On a holiday it returns the
        previous session unchanged. If the job stamped its own execution date on
        that payload, a four-day closure would produce four identical sessions
        under four different dates — each looking perfectly valid afterwards.

        Here we take a genuine payload and relabel it with a holiday date, which
        is exactly what such a bug would produce, and assert it is rejected.
        """
        provider = await registry.get(session, "TWSE")
        real = await provider.get_daily_prices()

        new_year = date(2026, 1, 1)
        forged = ProviderResult(
            records=[{**r, "trading_date": new_year} for r in real.records],
            metadata=SourceMetadata(
                source="TWSE",
                source_endpoint=real.metadata.source_endpoint,
                dataset="daily_prices",
                transport=Transport.REPLAY,
                source_request_at=datetime.now(UTC),
                data_as_of=new_year,
            ),
        )

        outcome = await IngestionPipeline(session).run(forged, dataset="daily_prices")
        await session.commit()

        assert outcome.records_written == 0
        assert outcome.records_quarantined == len(real.records)

        stored = (
            await session.execute(
                select(func.count())
                .select_from(DailyPrice)
                .where(DailyPrice.trading_date == new_year)
            )
        ).scalar_one()
        assert stored == 0, "a closed session must not appear in daily_prices"

        # And the rejection is explained rather than silent.
        q = (
            await session.execute(
                select(DataQuarantine).where(DataQuarantine.trading_date == new_year).limit(1)
            )
        ).scalar_one()
        assert "CAL01" in q.rule_ids
        assert "not a trading day" in q.errors[0]["message"]

    async def test_unknown_calendar_date_is_quarantined_not_assumed(
        self, session, registry, seeded_sources
    ):
        """With no calendar loaded at all, records are held rather than guessed."""
        provider = await registry.get(session, "TWSE")
        result = await provider.get_daily_prices()
        outcome = await IngestionPipeline(session).run(result, dataset="daily_prices")
        await session.commit()

        assert outcome.records_written == 0
        assert outcome.records_quarantined == result.record_count
        q = (await session.execute(select(DataQuarantine).limit(1))).scalar_one()
        assert "CAL00" in q.rule_ids


# ========================================================== idempotency
class TestIdempotentIngestion:
    async def test_running_twice_does_not_duplicate(self, session, registry, calendar_ready):
        first = await jobs.ingest_daily_prices(session, registry)
        await session.commit()
        count_after_first = (
            await session.execute(select(func.count()).select_from(DailyPrice))
        ).scalar_one()

        second = await jobs.ingest_daily_prices(session, registry)
        await session.commit()
        count_after_second = (
            await session.execute(select(func.count()).select_from(DailyPrice))
        ).scalar_one()

        assert first.records_written == second.records_written
        assert count_after_first == count_after_second > 0

    async def test_three_runs_stay_stable(self, session, registry, calendar_ready):
        counts = []
        for _ in range(3):
            await jobs.ingest_daily_prices(session, registry)
            await jobs.ingest_index_quotes(session, registry)
            await jobs.ingest_institutional_flow(session, registry, trading_date=SNAPSHOT_DATE)
            await session.commit()
            row = []
            for model in (DailyPrice, IndexQuote, InstitutionalFlow):
                n = (await session.execute(select(func.count()).select_from(model))).scalar_one()
                row.append(n)
            counts.append(tuple(row))
        assert counts[0] == counts[1] == counts[2]
        assert all(c > 0 for c in counts[0])

    async def test_reingestion_updates_in_place(self, session, registry, calendar_ready):
        await jobs.ingest_daily_prices(session, registry)
        await session.commit()

        row = (
            await session.execute(
                select(DailyPrice).where(DailyPrice.trading_date == SNAPSHOT_DATE).limit(1)
            )
        ).scalar_one()
        original_id, symbol, original_close = row.id, row.symbol, row.close

        # Corrupt it as if an earlier run had written a wrong close. The value
        # must stay inside [low, high] or the CHECK constraint rejects it before
        # the upsert gets a chance — the constraint doing its job, but not what
        # this test is about.
        wrong_close = row.low
        row.close = wrong_close
        await session.commit()

        await jobs.ingest_daily_prices(session, registry)
        await session.commit()
        await session.refresh(row)

        assert row.id == original_id, "upsert must update, not insert a new row"
        assert row.close == original_close, "re-ingestion must restore the true value"
        assert row.symbol == symbol

    async def test_calendar_sync_is_idempotent(self, session, registry, calendar_ready):
        before = (
            await session.execute(select(func.count()).select_from(TradingCalendar))
        ).scalar_one()
        await jobs.ingest_trading_calendar(session, registry, year=2026)
        await session.commit()
        after = (
            await session.execute(select(func.count()).select_from(TradingCalendar))
        ).scalar_one()
        assert before == after == 365


# ============================================================ quarantine
class TestQuarantine:
    async def test_invalid_ohlc_is_quarantined_not_dropped(self, session, registry, calendar_ready):
        bad = ProviderResult(
            records=[
                {
                    "symbol": "9999",
                    "market": "TWSE",
                    "trading_date": SNAPSHOT_DATE,
                    "open": Decimal("100"),
                    "high": Decimal("90"),  # high < low
                    "low": Decimal("95"),
                    "close": Decimal("98"),
                    "volume": 1000,
                    "source": "TWSE",
                },
                {
                    "symbol": "8888",
                    "market": "TWSE",
                    "trading_date": SNAPSHOT_DATE,
                    "open": Decimal("10"),
                    "high": Decimal("12"),
                    "low": Decimal("9"),
                    "close": Decimal("11"),
                    "volume": -5,  # negative volume
                    "source": "TWSE",
                },
            ],
            metadata=SourceMetadata(
                source="TWSE",
                source_endpoint="test://ohlc",
                dataset="daily_prices",
                transport=Transport.REPLAY,
                source_request_at=datetime.now(UTC),
                data_as_of=SNAPSHOT_DATE,
            ),
        )
        outcome = await IngestionPipeline(session).run(bad, dataset="daily_prices")
        await session.commit()

        assert outcome.records_written == 0
        assert outcome.records_quarantined == 2

        rows = (await session.execute(select(DataQuarantine))).scalars().all()
        assert len(rows) == 2
        by_symbol = {r.symbol: r for r in rows}
        assert "P10" in by_symbol["9999"].rule_ids  # high < low
        assert "P15" in by_symbol["8888"].rule_ids  # negative volume

        # The raw record survives, so the source payload can be inspected.
        assert by_symbol["9999"].raw_record["high"] == "90"
        assert by_symbol["9999"].ingestion_id is not None

    async def test_suspect_rows_are_stored_and_flagged_not_rejected(
        self, session, registry, calendar_ready
    ):
        """A 105% move is suspicious, not invalid. Deleting anomalous prices
        would delete real limit-up days, real crashes and real news reactions —
        the very events the platform exists to study."""
        prev = {"7777": Decimal("100")}
        result = ProviderResult(
            records=[
                {
                    "symbol": "7777",
                    "market": "TWSE",
                    "trading_date": SNAPSHOT_DATE,
                    "open": Decimal("190"),
                    "high": Decimal("205"),
                    "low": Decimal("189"),
                    "close": Decimal("205"),
                    "volume": 5000,
                    "source": "TWSE",
                }
            ],
            metadata=SourceMetadata(
                source="TWSE",
                source_endpoint="test://spike",
                dataset="daily_prices",
                transport=Transport.REPLAY,
                source_request_at=datetime.now(UTC),
                data_as_of=SNAPSHOT_DATE,
            ),
        )
        outcome = await IngestionPipeline(session).run(
            result, dataset="daily_prices", prev_closes=prev
        )
        await session.commit()

        assert outcome.records_written == 1
        assert outcome.records_quarantined == 0
        assert outcome.records_suspect == 1

        row = (
            await session.execute(select(DailyPrice).where(DailyPrice.symbol == "7777"))
        ).scalar_one()
        assert row.quality_status == "SUSPECT"
        assert "P30" in row.quality_flags


# ============================================================ provenance
class TestProvenance:
    async def test_every_accepted_record_has_source_and_timestamps(
        self, session, registry, calendar_ready
    ):
        await jobs.ingest_daily_prices(session, registry)
        await jobs.ingest_index_quotes(session, registry)
        await jobs.ingest_institutional_flow(session, registry, trading_date=SNAPSHOT_DATE)
        await session.commit()

        for model in (DailyPrice, IndexQuote, InstitutionalFlow):
            rows = (await session.execute(select(model))).scalars().all()
            assert rows, model.__tablename__
            for r in rows:
                assert r.source, f"{model.__tablename__} row without a source"
                assert r.ingested_at is not None
                assert r.ingested_at.tzinfo is not None, "timestamps must be tz-aware"
                assert r.ingestion_id is not None, "row not linked to its ingestion"

    async def test_ingestion_record_answers_where_when_and_what(
        self, session, registry, calendar_ready
    ):
        await jobs.ingest_daily_prices(session, registry)
        await session.commit()

        row = (await session.execute(select(DailyPrice).limit(1))).scalar_one()
        ingestion = await session.get(RawIngestion, row.ingestion_id)

        assert ingestion.source == "TWSE"  # where from
        assert ingestion.source_endpoint.startswith("https://")
        assert ingestion.source_request_at is not None  # when requested
        assert ingestion.ingested_at is not None  # when stored
        assert ingestion.data_as_of == row.trading_date  # what day it represents
        assert ingestion.response_hash and len(ingestion.response_hash) == 64
        assert ingestion.record_count >= 1

    async def test_replayed_transport_is_recorded_honestly(self, session, registry, calendar_ready):
        """Replayed responses are genuine TWSE data, but provenance must not
        imply an HTTP request that never happened."""
        await jobs.ingest_daily_prices(session, registry)
        await session.commit()
        ingestion = (
            await session.execute(
                select(RawIngestion).where(RawIngestion.dataset == "daily_prices").limit(1)
            )
        ).scalar_one()
        assert ingestion.transport == "REPLAY"
        assert ingestion.source == "TWSE"


# ==================================================== corporate actions
class TestCorporateActions:
    async def test_raw_price_is_immutable_and_adjustment_is_separate(
        self, session, registry, calendar_ready
    ):
        await jobs.ingest_daily_prices(session, registry, symbol="2330", month=HISTORY_MONTH)
        await session.commit()

        row = (
            await session.execute(
                select(DailyPrice).where(
                    DailyPrice.symbol == "2330",
                    DailyPrice.trading_date == date(2026, 7, 17),
                )
            )
        ).scalar_one()

        raw_close = row.close
        assert raw_close == Decimal("2290.0000")
        # Adjustment has not run: the derived columns are empty, and the raw
        # price is untouched. They are different columns by design.
        assert row.adjusted_close is None
        assert row.adjust_factor is None

        # Applying an adjustment must not overwrite what the exchange published.
        row.adjust_factor = Decimal("0.95")
        row.adjusted_close = raw_close * Decimal("0.95")
        await session.commit()
        await session.refresh(row)

        assert row.close == raw_close, "raw close must never be overwritten"
        assert row.adjusted_close == Decimal("2175.5000")

    async def test_corporate_actions_are_bitemporal(self, session, calendar_ready):
        session.add(
            CorporateAction(
                symbol="2330",
                market="TWSE",
                action_type="CASH_DIVIDEND",
                ex_date=date(2026, 6, 18),
                announced_at=datetime(2026, 5, 20, 9, 0, tzinfo=UTC),
                cash_dividend=Decimal("4.5"),
                factor=Decimal("0.998"),
                source="TWSE",
            )
        )
        await session.commit()

        svc = DataAvailabilityService(session)
        before = await svc.corporate_actions_as_of(date(2026, 5, 15), symbol="2330")
        after = await svc.corporate_actions_as_of(date(2026, 5, 21), symbol="2330")
        assert before == []
        assert len(after) == 1


# ========================================================== availability
class TestDataAvailability:
    async def test_available_at_uses_knowledge_time(self, session, calendar_ready):
        """The canonical example: a Q1 report disclosed on 2026-05-20 was not
        available on 2026-05-15, whatever its period says."""
        session.add(
            CorporateAction(
                symbol="2330",
                market="TWSE",
                action_type="CASH_DIVIDEND",
                ex_date=date(2026, 3, 31),
                announced_at=datetime(2026, 5, 20, 10, 0, tzinfo=UTC),
                cash_dividend=Decimal("3.0"),
                factor=Decimal("1"),
                source="TWSE",
            )
        )
        await session.commit()
        svc = DataAvailabilityService(session)

        assert (
            await svc.available_at("corporate_actions", date(2026, 5, 15), symbol="2330")
        ).available is False
        assert (
            await svc.available_at("corporate_actions", date(2026, 5, 21), symbol="2330")
        ).available is True

    async def test_prices_as_of_excludes_later_sessions(self, session, registry, calendar_ready):
        await jobs.ingest_daily_prices(session, registry, symbol="2330", month=HISTORY_MONTH)
        await session.commit()

        svc = DataAvailabilityService(session)
        mid_july = await svc.prices_as_of(date(2026, 7, 15), symbols=["2330"])
        end_july = await svc.prices_as_of(date(2026, 7, 31), symbols=["2330"])

        assert max(p.trading_date for p in mid_july) <= date(2026, 7, 15)
        assert len(end_july) > len(mid_july)
        assert all(p.trading_date <= date(2026, 7, 15) for p in mid_july)

    async def test_universe_includes_delisted_by_default(self, session, calendar_ready):
        """Building a universe from currently-listed companies is survivorship
        bias: it excludes exactly the companies a strategy needed to avoid."""
        today = date(2026, 8, 14)
        session.add_all(
            [
                StockMaster(
                    symbol="1101",
                    market="TWSE",
                    name="台泥",
                    source="TWSE",
                    listing_date=date(1962, 2, 9),
                    valid_from=date(2026, 1, 1),
                    is_current=True,
                    status="LISTED",
                ),
                StockMaster(
                    symbol="9999",
                    market="TWSE",
                    name="已下市公司",
                    source="TWSE",
                    listing_date=date(1990, 1, 1),
                    delisting_date=date(2026, 3, 1),
                    valid_from=date(2026, 1, 1),
                    is_current=True,
                    status="DELISTED",
                ),
            ]
        )
        await session.commit()
        svc = DataAvailabilityService(session)

        with_delisted = await svc.universe_as_of(today)
        without = await svc.universe_as_of(today, include_delisted=False)

        assert "9999" in with_delisted
        assert "9999" not in without
        assert "1101" in with_delisted and "1101" in without
