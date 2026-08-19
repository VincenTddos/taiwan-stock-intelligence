"""The quant pipeline against a real database: compute, persist, serve.

What is being established here is not that the arithmetic is right — the golden
tests do that, on TA-Lib, with no database in sight. It is that the numbers
survive the round trip with everything needed to interpret them: the feature
version that defines them, the price basis they were computed on, and an honest
account of what could not be computed and why.

The last one carries most of the weight. Thirty-seven of the fifty-three
declared factors need data this platform does not ingest yet. A pipeline that
quietly produced sixteen would look complete.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.market import DailyPrice, IndexQuote, StockMaster
from app.models.quant import (
    FactorScore,
    FactorValidityRecord,
    FeatureVersion,
    TechnicalIndicator,
)
from app.quant import factors as fct
from app.quant import indicators as ind
from app.quant import jobs
from app.quant.factors.validation import Verdict
from tests.conftest import login

pytestmark = pytest.mark.integration

MARKET = "TWSE"
SESSIONS = 320
LAST = date(2026, 8, 14)
SECTORS = ("半導體業", "金融保險業", "塑膠工業", "航運業")


def _sessions(count: int, end: date) -> list[date]:
    """`count` weekday sessions ending at `end`.

    Weekdays rather than calendar days because the panel's windows are counted
    in sessions; a Saturday in the series would make every window a day short in
    a way that is invisible in the output.
    """
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


@pytest.fixture
async def price_history(session):
    """Thirty symbols with 320 sessions of deterministic prices.

    Deterministic and generated rather than recorded: the recorded fixtures hold
    one session of the whole market, which is not enough history for a single
    250-day window, and the point of these tests is the pipeline rather than the
    values.
    """
    days = _sessions(SESSIONS, LAST)
    symbols = [f"{1101 + i}" for i in range(30)]

    index_level = 18_000.0
    for n, day in enumerate(days):
        index_level *= 1.0 + 0.004 * math.sin(n / 9.0)
        session.add(
            IndexQuote(
                index_code=jobs.BENCHMARK_INDEX,
                index_name="發行量加權股價指數",
                market=MARKET,
                trading_date=day,
                close=round(index_level, 2),
                source="TWSE",
            )
        )

    for index, symbol in enumerate(symbols):
        session.add(
            StockMaster(
                symbol=symbol,
                market=MARKET,
                name=f"測試{symbol}",
                sector=SECTORS[index % len(SECTORS)],
                shares_outstanding=int(1e8 * (index + 1)),
                valid_from=days[0],
                is_current=True,
                source="TWSE",
            )
        )
        price = 40.0 + index * 3.0
        for n, day in enumerate(days):
            price *= 1.0 + 0.012 * math.sin((n + index * 7) / 11.0)
            close = round(price, 2)
            session.add(
                DailyPrice(
                    symbol=symbol,
                    market=MARKET,
                    trading_date=day,
                    open=close,
                    high=round(close * 1.01, 2),
                    low=round(close * 0.99, 2),
                    close=close,
                    volume=1_000_000 + index * 1_000,
                    turnover=round(close * (1_000_000 + index * 1_000), 2),
                    source="TWSE",
                )
            )
    await session.commit()
    return {"symbols": symbols, "days": days}


class TestFeatureVersion:
    async def test_the_version_is_derived_from_the_specification(self, session):
        """Content-addressed, so a changed threshold cannot reuse a version.

        A hand-bumped number is a number someone forgets to bump on the one
        change that mattered — and every score already written under the old
        version would then be indistinguishable from the new ones.
        """
        first = jobs.version_string()
        second = jobs.version_string()
        assert first == second

        document = jobs.spec_document()
        document["thresholds"] = {**document["thresholds"], "abs_ic": 0.05}
        assert jobs.version_string(document) != first

    async def test_registering_twice_records_one_row(self, session):
        version = await jobs.register_feature_version(session)
        again = await jobs.register_feature_version(session)
        await session.commit()
        assert version == again

        rows = (
            await session.execute(
                select(func.count())
                .select_from(FeatureVersion)
                .where(FeatureVersion.version == version)
            )
        ).scalar_one()
        assert rows == 1

    async def test_the_specification_is_stored_in_full(self, session):
        version = await jobs.register_feature_version(session)
        await session.commit()
        row = (
            await session.execute(select(FeatureVersion).where(FeatureVersion.version == version))
        ).scalar_one()

        assert len(row.spec["indicators"]) == len(ind.REGISTRY)
        assert len(row.spec["factors"]) == len(fct.REGISTRY)
        stored = {f["name"]: f for f in row.spec["factors"]}
        # Every parameter that changes a number is in the record, or the version
        # documents nothing.
        momentum = stored["momentum_12_1"]
        assert momentum["lookback_days"] == 252
        assert momentum["winsorize"] == [0.01, 0.99]
        assert momentum["neutralize"] == ["sector", "size"]
        assert row.code_version


class TestComputation:
    async def test_a_session_produces_indicators_and_factors(self, session, price_history):
        report = await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()

        assert report.symbols == 30
        assert report.indicator_rows == 30
        assert report.factor_rows > 0

        row = (
            await session.execute(
                select(TechnicalIndicator).where(
                    TechnicalIndicator.symbol == "1101", TechnicalIndicator.trading_date == LAST
                )
            )
        ).scalar_one()
        assert row.feature_version == report.feature_version
        assert "rsi_14" in row.indicators
        assert "ma_240" in row.indicators

    async def test_unadjusted_prices_are_recorded_as_such(self, session, price_history):
        """The compromise is written down, not assumed.

        No corporate action coverage exists for these symbols, so the adjustment
        guard refuses and the panel is raw. Every row produced says `RAW`, and
        the report says why — because an indicator that cannot state its price
        basis is one nobody can check against a chart later.
        """
        report = await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()

        assert report.price_basis == "RAW"
        assert report.adjustment_blocked is not None
        assert "cannot be adjusted" in report.adjustment_blocked

        bases = (
            (await session.execute(select(TechnicalIndicator.price_basis).distinct()))
            .scalars()
            .all()
        )
        assert set(bases) == {"RAW"}

    async def test_factors_without_inputs_are_named_not_dropped(self, session, price_history):
        report = await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()

        assert report.factors_computed + len(report.factors_not_computable) == len(fct.REGISTRY)
        assert "earnings_yield" in report.factors_not_computable
        assert report.factors_not_computable["earnings_yield"] == ["eps_ttm"]
        assert "news_sentiment_7d" in report.factors_not_computable

    async def test_running_twice_changes_nothing(self, session, price_history):
        await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()
        first_indicators = (
            await session.execute(select(func.count()).select_from(TechnicalIndicator))
        ).scalar_one()
        first_factors = (
            await session.execute(select(func.count()).select_from(FactorScore))
        ).scalar_one()

        await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()
        assert (
            await session.execute(select(func.count()).select_from(TechnicalIndicator))
        ).scalar_one() == first_indicators
        assert (
            await session.execute(select(func.count()).select_from(FactorScore))
        ).scalar_one() == first_factors

    async def test_the_cross_section_is_standardised_across_symbols(self, session, price_history):
        """A z-score is only a z-score if it was taken across the universe."""
        await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()

        rows = (
            (
                await session.execute(
                    select(FactorScore).where(
                        FactorScore.factor_name == "log_market_cap",
                        FactorScore.trading_date == LAST,
                    )
                )
            )
            .scalars()
            .all()
        )
        scores = [float(r.zscore) for r in rows if r.zscore is not None]
        assert len(scores) == 30
        assert abs(sum(scores) / len(scores)) < 1e-6
        percentiles = [float(r.percentile) for r in rows if r.percentile is not None]
        assert min(percentiles) == pytest.approx(0.0)
        assert max(percentiles) == pytest.approx(1.0)

    async def test_direction_is_applied_to_the_score_and_not_the_raw_value(
        self, session, price_history
    ):
        """`hv_60` is volatility; its score is negative volatility.

        Storing both is what lets a stored raw value be checked against a
        published one while the score still means "higher is better".
        """
        await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()

        rows = (
            (
                await session.execute(
                    select(FactorScore).where(
                        FactorScore.factor_name == "hv_60", FactorScore.trading_date == LAST
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows
        assert all(r.raw_value is None or r.raw_value >= 0 for r in rows)
        paired = [(float(r.zscore), float(r.score)) for r in rows if r.zscore is not None]
        assert paired
        assert all(z == pytest.approx(-s) for z, s in paired)


class TestValidation:
    async def test_a_short_window_is_reported_as_insufficient_not_as_a_result(
        self, session, price_history
    ):
        """Forty dates produce an IC. They do not produce evidence."""
        reports = await jobs.evaluate_factors(
            session, start=LAST - timedelta(days=60), end=LAST, horizon=20
        )
        await session.commit()

        by_name = {r.factor: r for r in reports}
        assert by_name["momentum_12_1"].verdict is Verdict.INSUFFICIENT_DATA
        assert by_name["momentum_12_1"].dates < 60

    async def test_every_declared_factor_gets_a_verdict(self, session, price_history):
        reports = await jobs.evaluate_factors(
            session, start=LAST - timedelta(days=200), end=LAST, horizon=20
        )
        await session.commit()
        assert len(reports) == len(fct.REGISTRY)

        by_name = {r.factor: r for r in reports}
        assert by_name["earnings_yield"].verdict is Verdict.NOT_COMPUTABLE
        assert by_name["earnings_yield"].missing_inputs == ["eps_ttm"]

    async def test_verdicts_are_persisted_with_their_window(self, session, price_history):
        start = LAST - timedelta(days=200)
        await jobs.evaluate_factors(session, start=start, end=LAST, horizon=20)
        await session.commit()

        row = (
            await session.execute(
                select(FactorValidityRecord).where(
                    FactorValidityRecord.factor_name == "momentum_12_1"
                )
            )
        ).scalar_one()
        assert row.evaluated_from == start
        assert row.evaluated_to == LAST
        assert row.horizon_days == 20
        assert row.metrics["factor"] == "momentum_12_1"


class TestQuantApi:
    async def test_the_indicator_catalogue_describes_the_running_code(self, client):
        body = (await client.get("/api/v1/quant/indicators/catalogue")).json()
        assert len(body["data"]) == len(ind.REGISTRY)
        names = {row["name"] for row in body["data"]}
        assert {"rsi_14", "macd", "beta_250", "amihud_illiquidity_20"} <= names
        rsi = next(row for row in body["data"] if row["name"] == "rsi_14")
        assert rsi["inputs"] == ["close"]
        assert rsi["lookback_sessions"] == 15

    async def test_the_factor_catalogue_includes_what_cannot_be_computed(self, client):
        body = (await client.get("/api/v1/quant/factors/catalogue")).json()
        assert len(body["data"]) == len(fct.REGISTRY)
        by_name = {row["name"]: row for row in body["data"]}
        assert by_name["earnings_yield"]["inputs"] == ["eps_ttm"]
        assert by_name["momentum_12_1"]["direction"] == 1
        assert by_name["momentum_1m"]["direction"] == -1

    async def test_indicators_for_a_symbol(self, client, session, price_history):
        await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()

        resp = await client.get("/api/v1/stocks/1101/indicators")
        body = resp.json()
        assert resp.status_code == 200
        assert body["data"]["symbol"] == "1101"
        assert body["data"]["price_basis"] == "RAW"
        assert body["meta"]["feature_version"]
        assert "rsi_14" in body["data"]["indicators"]

    async def test_a_symbol_with_no_indicators_says_so(self, client, price_history):
        resp = await client.get("/api/v1/stocks/9999/indicators")
        assert resp.status_code == 404
        assert resp.json()["type"].endswith("/data-not-available")

    async def test_an_unknown_indicator_name_is_rejected(self, client, session, price_history):
        await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()
        resp = await client.get("/api/v1/stocks/1101/indicators?names=rsi_14,not_an_indicator")
        assert resp.status_code == 422

    async def test_factor_ranking_orders_by_score(self, client, session, price_history):
        await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()

        body = (await client.get("/api/v1/factors/ranking?factor=log_market_cap&limit=5")).json()
        scores = [row["score"] for row in body["data"]]
        assert scores == sorted(scores, reverse=True)
        assert body["data"][0]["rank"] == 1
        # `log_market_cap` has direction -1, so the best score is the smallest
        # company. A ranking that put the largest first would mean direction was
        # lost somewhere between the pipeline and the endpoint.
        raw_values = [float(row["raw_value"]) for row in body["data"]]
        assert raw_values == sorted(raw_values)

    async def test_the_screener_returns_the_values_that_passed(
        self, client, session, price_history
    ):
        await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()

        resp = await client.post(
            "/api/v1/screener",
            json={
                "conditions": [
                    {"factor": "log_market_cap", "field": "percentile", "op": "gte", "value": 0.5}
                ],
                "limit": 50,
            },
        )
        body = resp.json()
        assert resp.status_code == 200
        assert body["data"]
        for row in body["data"]:
            # Decimals cross the wire as strings, as everywhere else in this
            # API — a float would silently round a price.
            assert float(row["values"]["log_market_cap"]["percentile"]) >= 0.5

    async def test_the_screener_refuses_a_factor_it_has_no_scores_for(
        self, client, session, price_history
    ):
        """An empty result and an uncomputed factor must not look alike."""
        await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()

        resp = await client.post(
            "/api/v1/screener",
            json={"conditions": [{"factor": "earnings_yield", "op": "gt", "value": 0}]},
        )
        assert resp.status_code == 422
        assert "earnings_yield" in resp.json()["detail"]

    async def test_the_screener_rejects_an_unknown_factor(self, client):
        resp = await client.post(
            "/api/v1/screener",
            json={"conditions": [{"factor": "vibes", "op": "gt", "value": 0}]},
        )
        assert resp.status_code == 422


class TestNoMarketDataLeaksIntoTheQuantPath:
    async def test_the_api_never_computes_on_the_request_path(self, client, price_history):
        """No indicators were computed, so the endpoint must 404 rather than compute.

        The rule is `Frontend → FastAPI → PostgreSQL`. An endpoint that computed
        on demand would take a minute, would depend on how much history it
        happened to load, and would let two users see different numbers for the
        same session.
        """
        resp = await client.get("/api/v1/stocks/1101/indicators")
        assert resp.status_code == 404

    async def test_an_admin_can_read_the_catalogue_without_data(self, client, admin_user):
        token = await login(client, "admin@test.dev", "admin-password-123")
        resp = await client.get(
            "/api/v1/quant/factors/catalogue", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200


class TestPerformanceShape:
    async def test_a_full_session_computes_inside_the_budget(self, session, price_history):
        """`QUANT_ENGINE.md` §9 budgets 180 s for a full market session.

        Thirty symbols is not a full market, so this asserts the shape rather
        than the number: the run reports its own timings, and they are recorded
        so a regression that makes the job ten times slower is visible in CI
        rather than discovered in production.
        """
        started = datetime.now(UTC)
        report = await jobs.compute_for_date(session, trading_date=LAST)
        await session.commit()
        elapsed = (datetime.now(UTC) - started).total_seconds()

        assert report.timings_ms["total"] > 0
        assert set(report.timings_ms) >= {"load", "indicators", "factors", "persist", "total"}
        assert elapsed < 60, "30 symbols should not take a minute"
