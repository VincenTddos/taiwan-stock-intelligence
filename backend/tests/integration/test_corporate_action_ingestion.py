"""Corporate action ingestion, end to end on recorded exchange responses.

Two things are being established here, and only one of them is about rows
landing in a table.

The first is that a replayed run does not reach the exchange. The corporate
action providers were resolved without consulting `PROVIDER_MODE` until this
work, so a suite that believed it was replaying was quietly making live HTTP
calls for this one dataset — and would have passed either way, which is what
made it worth a test rather than a fix alone.

The second is that coverage is recorded for every symbol that was *searched*,
not for every symbol something was *found* for. That distinction is the entire
purpose of `corporate_action_coverage`: without it, a company that paid no
dividend and a company nobody ever looked up are the same empty set, and
adjusting on the first reading when the truth is the second removes a real
adjustment while leaving a series that looks perfectly ordinary.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.ingest import jobs
from app.models.market import (
    CorporateAction,
    CorporateActionCoverage,
    CorporateActionType,
    StockMaster,
)
from app.models.ops import RawIngestion, Transport
from app.services.corporate_action_service import (
    AdjustmentNotPermitted,
    CorporateActionCoverageService,
)

pytestmark = pytest.mark.integration

# The recordings: TWT49U covers 2026, TWTAUU covers 2019 onwards, TWT48U is a
# forward-looking snapshot taken on 2026-08-17.
RANGE = (date(2026, 1, 1), date(2026, 12, 31))


@pytest.fixture
async def master(session, registry, seeded_sources):
    """The security master, so coverage has a universe to be recorded over."""
    await jobs.ingest_stock_master(session, registry)
    await session.commit()


async def _ingest(session, registry, source: str, start=RANGE[0], end=RANGE[1]):
    result = await jobs.ingest_corporate_actions(
        session, registry, start=start, end=end, source=source
    )
    await session.commit()
    return result


class TestReplayedIngestion:
    async def test_nothing_reaches_the_exchange_in_replay_mode(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        await _ingest(session, registry, "TWSE_EXRIGHT_RESULTS")
        transports = (
            await session.execute(
                select(RawIngestion.transport).where(RawIngestion.dataset == "corporate_actions")
            )
        ).scalars()
        recorded = set(transports)
        assert recorded == {Transport.REPLAY}, (
            "a replayed run must not make live calls, and must not claim to have "
            f"made them either — got {recorded}"
        )

    async def test_actions_are_written_with_the_provider_as_their_source(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        await _ingest(session, registry, "TWSE_EXRIGHT_RESULTS")
        rows = (await session.execute(select(CorporateAction))).scalars().all()
        assert rows
        assert {r.source for r in rows} == {"TWSE_EXRIGHT_RESULTS"}
        assert {r.action_type for r in rows} == {str(CorporateActionType.CASH_DIVIDEND)}
        assert all(r.reference_price_before is not None for r in rows), (
            "the exchange's own reference prices are what every factor is checked "
            "against, so they must survive ingestion"
        )

    async def test_the_same_event_from_two_sources_does_not_collide(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        """The natural key includes the source, and it has to.

        The forecast table and the results table both report cash dividends. If
        they shared a source key one would overwrite the other, and the coverage
        row for whichever lost would describe action types it no longer holds.
        """
        await _ingest(session, registry, "TWSE_EXRIGHT_RESULTS")
        await _ingest(session, registry, "TWSE_EXRIGHT_FORECAST")
        sources = (await session.execute(select(CorporateAction.source).distinct())).scalars()
        assert "TWSE_EXRIGHT_RESULTS" in set(sources)

    async def test_ingesting_twice_writes_the_same_rows(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        first = await _ingest(session, registry, "TWSE_EXRIGHT_RESULTS")
        count = (
            await session.execute(select(func.count()).select_from(CorporateAction))
        ).scalar_one()
        second = await _ingest(session, registry, "TWSE_EXRIGHT_RESULTS")
        again = (
            await session.execute(select(func.count()).select_from(CorporateAction))
        ).scalar_one()
        assert count == again
        assert first["outcome"]["records_in"] == second["outcome"]["records_in"]

    async def test_undecomposable_rows_are_quarantined_not_dropped(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        """A 權 row is a real event this endpoint cannot quantify.

        Dropping it would make a quarter with an unsplittable event look like a
        quarter with no event at all.
        """
        result = await _ingest(session, registry, "TWSE_EXRIGHT_RESULTS")
        assert result["outcome"]["records_quarantined"] > 0


class TestCoverageIsRecordedForWhatWasSearched:
    async def test_every_listed_symbol_is_covered_not_only_the_ones_with_actions(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        await _ingest(session, registry, "TWSE_EXRIGHT_RESULTS")
        listed = (
            await session.execute(
                select(func.count()).select_from(StockMaster).where(StockMaster.is_current)
            )
        ).scalar_one()
        rows = (await session.execute(select(CorporateActionCoverage))).scalars().all()
        assert len(rows) == listed

        # The recorded master (1101–1103) and the recorded ex-rights table
        # (2330 and two bond ETFs) do not overlap, so every coverage row here is
        # a symbol that was searched and had nothing — which is the case the
        # table exists for and the one an actions-only view cannot express.
        assert all(r.actions_found == 0 for r in rows)
        assert {r.symbol for r in rows} == {"1101", "1102", "1103"}

    async def test_a_forward_looking_source_does_not_claim_the_past(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        """TWT48U is a snapshot of upcoming ex-dates and carries no history.

        Asked about 2019 it answers with nothing, and recording that as coverage
        would convert "cannot see the past" into "nothing happened".
        """
        result = await _ingest(
            session, registry, "TWSE_EXRIGHT_FORECAST", date(2019, 1, 1), date(2019, 12, 31)
        )
        assert result["covered"] is None
        assert result["coverage_symbols"] == 0

        rows = (
            (
                await session.execute(
                    select(CorporateActionCoverage).where(
                        CorporateActionCoverage.source == "TWSE_EXRIGHT_FORECAST"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    async def test_a_source_records_only_the_action_types_it_can_see(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        await _ingest(session, registry, "TWSE_EXRIGHT_RESULTS")
        row = (
            await session.execute(
                select(CorporateActionCoverage).where(CorporateActionCoverage.symbol == "1101")
            )
        ).scalar_one()
        assert row.action_types == [str(CorporateActionType.CASH_DIVIDEND)]

    async def test_one_source_alone_is_not_enough_to_adjust(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        """Dividends-only coverage leaves a capital reduction unaccounted for."""
        await _ingest(session, registry, "TWSE_EXRIGHT_RESULTS")
        svc = CorporateActionCoverageService(session)
        with pytest.raises(AdjustmentNotPermitted, match="never searched for"):
            await svc.assert_adjustable(["1101"], date(2026, 2, 1), date(2026, 8, 1))

    async def test_the_sources_together_open_the_guard(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        """What the whole arrangement is for: coverage unions across sources.

        The results table brings cash dividends with history, the reduction
        table brings capital reductions, and the forecast table brings stock
        dividends and rights issues from today forward. Only together do they
        cover every price-affecting class TWSE actually publishes.
        """
        today = date.today()
        for source in (
            "TWSE_EXRIGHT_RESULTS",
            "TWSE_CAPITAL_REDUCTION",
            "TWSE_EXRIGHT_FORECAST",
        ):
            await _ingest(session, registry, source, date(2019, 1, 1), today)

        svc = CorporateActionCoverageService(session)
        # The forecast table cannot reach back, so the window that all three
        # cover starts today. That is a real limit, and the guard states it.
        await svc.assert_adjustable(["1101"], today, today)

        with pytest.raises(AdjustmentNotPermitted, match="does not span"):
            await svc.assert_adjustable(["1101"], date(2019, 1, 1), today)


class TestRequestsAreShapedByTheSource:
    def test_a_range_is_split_to_what_the_source_will_answer(self):
        """TWSE's capital reduction endpoint stops responding on long ranges.

        Measured on 2026-08-19: 2.9 s for a year-to-date, 30.6 s for one full
        year, no response at all inside 90 s for eight. So the span is declared
        by the provider and the job splits on it — a timeout on a backfill looks
        like an outage rather than like a query that asked for too much.
        """
        chunks = jobs._chunks(date(2019, 1, 1), date(2026, 12, 31), 366)
        assert len(chunks) == 8
        assert chunks[0][0] == date(2019, 1, 1)
        assert chunks[-1][1] == date(2026, 12, 31)
        # Contiguous, no overlap: an overlap would re-fetch and a gap would be
        # coverage claimed over dates nobody asked about.
        for (_, earlier_end), (later_start, _) in itertools.pairwise(chunks):
            assert later_start == earlier_end + timedelta(days=1)
        assert all((e - s).days < 366 for s, e in chunks)

    def test_an_unbounded_source_is_asked_once(self):
        assert jobs._chunks(date(2019, 1, 1), date(2026, 12, 31), None) == [
            (date(2019, 1, 1), date(2026, 12, 31))
        ]

    async def test_a_chunked_run_records_one_provenance_row_per_request(
        self, session, registry, seeded_corporate_action_sources, master
    ):
        """Chunking must not collapse provenance.

        The job returns one summary, but `raw_ingestions` is one row per call —
        otherwise a response hash would describe bytes that were never a single
        response.
        """
        await _ingest(
            session, registry, "TWSE_CAPITAL_REDUCTION", date(2019, 1, 1), date(2026, 12, 31)
        )
        rows = (
            (
                await session.execute(
                    select(RawIngestion).where(RawIngestion.source == "TWSE_CAPITAL_REDUCTION")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 8, "eight calendar years, eight requests"
        assert all(r.response_hash for r in rows)

    async def test_the_registry_row_configures_the_transport(self, session, seeded_sources):
        """A timeout is meant to be a row update, not a deploy.

        Corporate action providers were constructed bare, so every one of them
        used its own hard-coded default and the registry's configuration applied
        to prices only. The capital reduction endpoint is the case that made it
        matter: it needs three times the default timeout.
        """
        from app.core.config import get_settings
        from app.models.ops import DataSource, SourceStatus
        from app.providers.registry import ProviderRegistry

        session.add(
            DataSource(
                code="TWSE_CAPITAL_REDUCTION",
                name="reductions",
                provider_type="TWSE_CAPITAL_REDUCTION",
                base_url="https://www.twse.com.tw/rwd/zh",
                market="TWSE",
                rate_limit_per_minute=42,
                max_concurrency=2,
                min_interval_ms=350,
                timeout_seconds=60,
                max_retries=5,
                status=SourceStatus.ACTIVE,
            )
        )
        await session.commit()

        live = get_settings().model_copy(update={"PROVIDER_MODE": "live"})
        provider = await ProviderRegistry(live).get_corporate_actions(
            session, "TWSE_CAPITAL_REDUCTION"
        )
        fetcher = provider.fetcher
        assert fetcher.max_retries == 5
        assert fetcher.rate_limit.requests_per_minute == 42
        # One budget per exchange, not one per endpoint: three corporate action
        # sources plus the price provider would otherwise be four budgets
        # against one exchange.
        assert fetcher.rate_limit.source == "TWSE"
        await provider.aclose()
