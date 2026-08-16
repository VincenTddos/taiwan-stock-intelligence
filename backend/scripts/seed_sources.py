"""Seed the data source registry and freshness contracts.

Only endpoints that were **actually verified against the live service** are
seeded as ACTIVE. Everything else is seeded UNVERIFIED, so the registry never
implies a capability that has not been demonstrated. Verification evidence is
recorded in `docs/DATA_SOURCES.md`.

Idempotent: safe to run on every deploy.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import dispose_engine, get_sessionmaker
from app.models.ops import DataFreshness, DataSource, SourceStatus
from app.providers.twse import OPENAPI_BASE, RWD_BASE

VERIFIED_AT = datetime(2026, 8, 15, tzinfo=UTC)

GOV_LICENCE = "政府資料開放授權條款第1版 (Open Government Data License, Taiwan v1.0)"

SOURCES: list[dict[str, object]] = [
    {
        "code": "TWSE",
        "name": "臺灣證券交易所 OpenAPI",
        "provider_type": "TWSE",
        "base_url": OPENAPI_BASE,
        "market": "TWSE",
        "frequency": "TRADING_DAY",
        "requires_auth": False,
        # No published limit. 60/min with a 350 ms floor is a deliberately
        # conservative guess for an unmetered public service — being a polite
        # client costs one extra minute per thousand requests.
        "rate_limit_per_minute": 60,
        "rate_limit_per_day": 20000,
        "max_concurrency": 2,
        "min_interval_ms": 350,
        "timeout_seconds": 20,
        "max_retries": 3,
        "status": SourceStatus.ACTIVE,
        "verified_at": VERIFIED_AT,
        "licence": GOV_LICENCE,
        "coverage": {
            "verified_endpoints": [
                "/exchangeReport/MI_INDEX",
                "/exchangeReport/STOCK_DAY_ALL",
                "/opendata/t187ap03_L",
                "/holidaySchedule/holidaySchedule",
            ],
            "datasets": ["index_quotes", "daily_prices", "stock_master", "trading_calendar"],
            "history": "snapshot only — these endpoints take no date parameter",
        },
        "notes": (
            "Snapshot endpoints. On a non-trading day they repeat the previous "
            "session with no indication of closure, so the trading date must come "
            "from the payload and be checked against the calendar."
        ),
    },
    {
        "code": "TWSE_RWD",
        "name": "臺灣證券交易所 盤後資訊 (date-parameterised)",
        "provider_type": "TWSE",
        "base_url": RWD_BASE,
        "market": "TWSE",
        "frequency": "TRADING_DAY",
        "requires_auth": False,
        "rate_limit_per_minute": 40,
        "rate_limit_per_day": 12000,
        "max_concurrency": 1,
        "min_interval_ms": 800,
        "timeout_seconds": 25,
        "max_retries": 3,
        "status": SourceStatus.ACTIVE,
        "verified_at": VERIFIED_AT,
        "licence": GOV_LICENCE,
        "coverage": {
            "verified_endpoints": [
                "/afterTrading/STOCK_DAY?date=&stockNo=",
                "/fund/T86?date=&selectType=ALL",
            ],
            "datasets": ["daily_prices", "institutional_flow"],
            "history": "date-parameterised — the only path for historical backfill",
        },
        "notes": (
            "T86 returns the whole market for one day (~2,500 requests for ten "
            "years). STOCK_DAY returns one symbol for one month (~120,000 for the "
            "same span) and is therefore only used to fill gaps."
        ),
    },
    {
        "code": "TPEX",
        "name": "證券櫃檯買賣中心 OpenAPI",
        "provider_type": "TPEX",
        "base_url": "https://www.tpex.org.tw/openapi/v1",
        "market": "TPEX",
        "frequency": "TRADING_DAY",
        "requires_auth": False,
        "rate_limit_per_minute": 40,
        "rate_limit_per_day": 12000,
        "max_concurrency": 1,
        "min_interval_ms": 800,
        "timeout_seconds": 25,
        "max_retries": 3,
        # Documented and publicly reachable, but every request from the build
        # environment returned 403. Not marked ACTIVE until observed working.
        "status": SourceStatus.UNVERIFIED,
        "licence": GOV_LICENCE,
        "coverage": {
            "documented_endpoints": [
                "/tpex_mainboard_daily_close_quotes",
                "/tpex_mainboard_peratio_analysis",
            ],
            "datasets": ["daily_prices", "valuation"],
        },
        "notes": (
            "Returned HTTP 403 from the build environment on 2026-08-15 — likely "
            "IP-based filtering rather than an endpoint problem. Re-verify from a "
            "Taiwanese network before enabling; no provider implementation exists yet."
        ),
    },
    {
        "code": "TAIFEX",
        "name": "臺灣期貨交易所 OpenAPI",
        "provider_type": "TAIFEX",
        "base_url": "https://openapi.taifex.com.tw",
        "market": "TAIFEX",
        "frequency": "TRADING_DAY",
        "requires_auth": False,
        "rate_limit_per_minute": 30,
        "max_concurrency": 1,
        "min_interval_ms": 1000,
        "status": SourceStatus.UNVERIFIED,
        "licence": GOV_LICENCE,
        "coverage": {"datasets": ["futures_institutional"], "needed_by": "Phase 5 market regime"},
        "notes": "Not required before Phase 5. Unverified; no implementation.",
    },
    {
        "code": "MOPS",
        "name": "公開資訊觀測站",
        "provider_type": "MOPS",
        "base_url": "https://mops.twse.com.tw",
        "market": "TWSE",
        "frequency": "EVENT",
        "requires_auth": False,
        "rate_limit_per_minute": 20,
        "max_concurrency": 1,
        "min_interval_ms": 2000,
        "status": SourceStatus.UNVERIFIED,
        "licence": "公開資訊，使用前需確認條款",
        "coverage": {
            "datasets": ["financials", "material_information", "corporate_actions"],
            "needed_by": "Phase 2 corporate actions, Phase 4 events",
        },
        "notes": (
            "Form-POST driven with HTML responses; more fragile than the OpenAPI "
            "sources. Prefer TWSE OpenAPI equivalents where they exist."
        ),
    },
]

FRESHNESS: list[dict[str, object]] = [
    {
        "dataset": "daily_prices",
        "market": "TWSE",
        "description": "個股日成交資訊",
        "expected_lag_minutes": 90,
        "expected_frequency": "TRADING_DAY",
        "status": "MISSING",
    },
    {
        "dataset": "index_quotes",
        "market": "TWSE",
        "description": "指數收盤行情",
        "expected_lag_minutes": 60,
        "expected_frequency": "TRADING_DAY",
        "status": "MISSING",
    },
    {
        "dataset": "institutional_flow",
        "market": "TWSE",
        "description": "三大法人買賣超",
        "expected_lag_minutes": 180,
        "expected_frequency": "TRADING_DAY",
        "status": "MISSING",
    },
    {
        "dataset": "stock_master",
        "market": "TWSE",
        "description": "上市公司基本資料",
        "expected_lag_minutes": 1440,
        "expected_frequency": "DAILY",
        "status": "MISSING",
    },
]


MUTABLE_COLUMNS = (
    "name",
    "provider_type",
    "base_url",
    "market",
    "coverage",
    "frequency",
    "requires_auth",
    "rate_limit_per_minute",
    "rate_limit_per_day",
    "max_concurrency",
    "min_interval_ms",
    "timeout_seconds",
    "max_retries",
    "status",
    "verified_at",
    "licence",
    "notes",
)


# Filled in for any row that omits them. A multi-row INSERT compiles one
# statement from the union of keys, so a row missing `timeout_seconds` would
# insert NULL into a NOT NULL column rather than picking up the model default.
COLUMN_DEFAULTS: dict[str, object] = {
    "market": None,
    "coverage": None,
    "frequency": None,
    "requires_auth": False,
    "rate_limit_per_minute": 60,
    "rate_limit_per_day": None,
    "max_concurrency": 2,
    "min_interval_ms": 0,
    "timeout_seconds": 20,
    "max_retries": 3,
    "status": SourceStatus.ACTIVE,
    "verified_at": None,
    "licence": None,
    "notes": None,
}


def _normalised() -> list[dict[str, object]]:
    """Give every row an identical key set, defaulting what is absent."""
    keys = {k for row in SOURCES for k in row} | set(COLUMN_DEFAULTS)
    return [{k: row.get(k, COLUMN_DEFAULTS.get(k)) for k in keys} for row in SOURCES]


async def seed() -> int:
    async with get_sessionmaker()() as session:
        stmt = pg_insert(DataSource).values(_normalised())
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[DataSource.code],
                set_={k: getattr(stmt.excluded, k) for k in MUTABLE_COLUMNS},
            )
        )

        fstmt = pg_insert(DataFreshness).values(FRESHNESS)
        await session.execute(fstmt.on_conflict_do_nothing(index_elements=[DataFreshness.dataset]))
        await session.commit()

    active = sum(1 for s in SOURCES if s["status"] == SourceStatus.ACTIVE)
    print(
        f"seeded {len(SOURCES)} data sources ({active} ACTIVE, {len(SOURCES) - active} UNVERIFIED)"
    )
    print(f"seeded {len(FRESHNESS)} freshness contracts")
    return 0


async def _main() -> int:
    try:
        return await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
