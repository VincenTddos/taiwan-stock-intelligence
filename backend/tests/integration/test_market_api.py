"""Market API.

The structural rule under test: the API serves from the database only. No
endpoint reaches an exchange, so response time and correctness do not depend on
an external service being up, and a user cannot burn the ingestion rate limit.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.ingest import jobs

pytestmark = pytest.mark.integration

SNAPSHOT_DATE = date(2026, 8, 14)


@pytest.fixture
async def market_data(session, registry, seeded_sources):
    """Ingest the recorded datasets so the API has something real to serve."""
    await jobs.ingest_trading_calendar(session, registry, year=2026)
    await jobs.ingest_stock_master(session, registry)
    await jobs.ingest_daily_prices(session, registry)
    await jobs.ingest_daily_prices(session, registry, symbol="2330", month=date(2026, 7, 1))
    await jobs.ingest_index_quotes(session, registry)
    await jobs.ingest_institutional_flow(session, registry, trading_date=SNAPSHOT_DATE)
    await jobs.refresh_market_status(session)
    await jobs.refresh_freshness(session)
    await session.commit()


class TestMarketStatus:
    async def test_status_reports_real_coverage(self, client, market_data):
        body = (await client.get("/api/v1/market/status")).json()
        data = body["data"]
        assert data["market"] == "TWSE"
        assert data["last_trading_date"] == "2026-08-14"
        assert data["price_row_count"] > 0
        assert body["meta"]["is_demo"] is False

    async def test_missing_market_says_so(self, client, market_data):
        resp = await client.get("/api/v1/market/status?market=NOPE")
        assert resp.status_code == 404
        assert resp.json()["type"].endswith("/data-not-available")

    async def test_calendar_endpoint(self, client, market_data):
        resp = await client.get("/api/v1/market/calendar?start=2026-02-09&end=2026-02-27")
        days = {d["date"]: d for d in resp.json()["data"]}
        assert days["2026-02-16"]["is_trading_day"] is False  # 春節
        assert days["2026-02-11"]["is_trading_day"] is True  # 春節前最後交易日
        assert days["2026-02-12"]["session_type"] == "SETTLEMENT_ONLY"


class TestStocks:
    async def test_list_and_search(self, client, market_data):
        body = (await client.get("/api/v1/stocks?q=台泥")).json()
        assert body["pagination"]["total"] >= 1
        assert body["data"][0]["symbol"] == "1101"
        assert body["data"][0]["industry_name"] == "水泥工業"

    async def test_detail_returns_master_record(self, client, market_data):
        """The recorded master sample covers 1101-1103; the price fixtures cover
        2330 and the 0040xA ETFs. That mismatch is a property of the recordings,
        not of the code — see tests/fixtures/twse/README.md."""
        body = (await client.get("/api/v1/stocks/1101")).json()
        assert body["data"]["symbol"] == "1101"
        assert body["data"]["short_name"] == "台泥"
        assert body["data"]["latest_price"] is None, "no price recorded for 1101"

    async def test_unknown_symbol_returns_not_available(self, client, market_data):
        resp = await client.get("/api/v1/stocks/9999")
        assert resp.status_code == 404


class TestPrices:
    async def test_returns_real_bars(self, client, market_data):
        body = (await client.get("/api/v1/stocks/2330/prices")).json()
        data = body["data"]
        assert data["count"] == 22
        assert data["adjusted"] is False

        bar = next(b for b in data["bars"] if b["trading_date"] == "2026-07-17")
        assert bar["open"] == "2375.0000"
        assert bar["close"] == "2290.0000"
        assert bar["volume"] == 97362670

    async def test_bars_are_chronological(self, client, market_data):
        bars = (await client.get("/api/v1/stocks/2330/prices")).json()["data"]["bars"]
        dates = [b["trading_date"] for b in bars]
        assert dates == sorted(dates)

    async def test_date_range_filter(self, client, market_data):
        body = (
            await client.get("/api/v1/stocks/2330/prices?start=2026-07-20&end=2026-07-24")
        ).json()
        assert body["data"]["count"] == 5

    async def test_no_data_is_404_not_an_empty_series(self, client, market_data):
        """An empty array would be indistinguishable from 'the market was shut'."""
        resp = await client.get("/api/v1/stocks/2330/prices?start=2019-01-01&end=2019-01-31")
        assert resp.status_code == 404
        assert resp.json()["type"].endswith("/data-not-available")

    async def test_adjusted_close_is_a_separate_field(self, client, market_data):
        bars = (await client.get("/api/v1/stocks/2330/prices")).json()["data"]["bars"]
        assert all("close" in b and "adjusted_close" in b for b in bars)
        assert all(b["adjusted_close"] is None for b in bars), "no adjustment run yet"


class TestIndices:
    async def test_returns_taiex_with_correct_sign(self, client, market_data):
        body = (await client.get("/api/v1/indices")).json()
        taiex = next(i for i in body["data"] if i["index_code"] == "TAIEX")
        assert taiex["close"] == "45811.0100"
        assert taiex["change"] == "-210.4700", "漲跌='-' must survive to the API"


class TestInstitutional:
    async def test_buy_sell_and_net_all_present(self, client, market_data):
        body = (await client.get("/api/v1/institutional?symbol=6770")).json()
        foreign = next(r for r in body["data"] if r["investor_type"] == "FOREIGN")
        assert foreign["buy_volume"] == 253882636
        assert foreign["sell_volume"] == 108916172
        assert foreign["net_volume"] == 144966464


class TestEnvelopeAndCache:
    async def test_every_market_response_carries_meta(self, client, market_data):
        for path in (
            "/api/v1/market/status",
            "/api/v1/stocks",
            "/api/v1/stocks/1101",
            "/api/v1/stocks/2330/prices",
            "/api/v1/indices",
            "/api/v1/institutional",
            "/api/v1/market/data-operations",
        ):
            body = (await client.get(path)).json()
            assert "meta" in body, path
            assert body["meta"]["is_demo"] is False, path
            assert body["meta"]["request_id"], path

    async def test_second_call_is_served_from_cache(self, client, redis_client, market_data):
        first = (await client.get("/api/v1/stocks/2330/prices")).json()
        second = (await client.get("/api/v1/stocks/2330/prices")).json()
        assert first["meta"]["cache"]["hit"] is False
        assert second["meta"]["cache"]["hit"] is True
        assert first["data"]["count"] == second["data"]["count"]

    async def test_cache_invalidation_by_version_bump(self, client, redis_client, market_data):
        from app.core.cache import bump_cache_version

        await client.get("/api/v1/stocks/2330/prices")
        assert (await client.get("/api/v1/stocks/2330/prices")).json()["meta"]["cache"]["hit"]

        await bump_cache_version(redis_client, "prices")
        after = (await client.get("/api/v1/stocks/2330/prices")).json()
        assert after["meta"]["cache"]["hit"] is False, "version bump must retire the entry"


class TestDataOperations:
    async def test_reports_datasets_sources_and_quarantine(self, client, market_data):
        data = (await client.get("/api/v1/market/data-operations")).json()["data"]

        datasets = {d["dataset"] for d in data["datasets"]}
        assert {"daily_prices", "index_quotes", "institutional_flow"} <= datasets

        sources = {s["code"]: s for s in data["sources"]}
        assert sources["TWSE"]["status"] == "ACTIVE"
        assert data["quarantine_total"] == 0
        assert data["overall"] in ("FRESH", "STALE", "DEGRADED", "MISSING")

    async def test_unverified_sources_are_declared_not_hidden(self, client, session, market_data):
        """The registry must not imply a capability that has not been shown to
        work. TPEx returned 403 from the build environment."""
        from app.models.ops import DataSource, SourceStatus

        session.add(
            DataSource(
                code="TPEX",
                name="TPEx OpenAPI",
                provider_type="TPEX",
                base_url="https://www.tpex.org.tw/openapi/v1",
                market="TPEX",
                status=SourceStatus.UNVERIFIED,
                rate_limit_per_minute=40,
                max_concurrency=1,
                min_interval_ms=800,
                timeout_seconds=25,
                max_retries=3,
                notes="403 from the build environment; re-verify from a TW network",
            )
        )
        await session.commit()

        data = (await client.get("/api/v1/market/data-operations")).json()["data"]
        tpex = next(s for s in data["sources"] if s["code"] == "TPEX")
        assert tpex["status"] == "UNVERIFIED"


class TestNoProviderCallsFromTheApi:
    async def test_api_serves_from_the_database_only(self, client, market_data, monkeypatch):
        """Structural guarantee: if any endpoint called a provider, this fails.

        We poison the provider constructors, then exercise every market route.
        """
        import app.providers.registry as registry_mod

        def _boom(*_a: object, **_k: object) -> object:
            raise AssertionError("an API request constructed a provider")

        monkeypatch.setattr(registry_mod, "TWSEProvider", _boom)
        monkeypatch.setattr(registry_mod, "ReplayProvider", _boom)

        for path in (
            "/api/v1/market/status",
            "/api/v1/market/calendar?start=2026-08-01&end=2026-08-31",
            "/api/v1/stocks",
            "/api/v1/stocks/1101",
            "/api/v1/stocks/2330/prices",
            "/api/v1/indices",
            "/api/v1/institutional",
            "/api/v1/market/data-operations",
        ):
            resp = await client.get(path)
            assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text[:200]}"
