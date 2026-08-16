"""Replay provider — serves recorded exchange responses through the live parsers.

This is **not** mock data. The bytes are genuine TWSE responses captured on
2026-08-15 and stored verbatim in `tests/fixtures/twse/`. They go through
exactly the same parsing code as a live fetch, so the records produced are real
market data.

What is *not* real is the transport, and that distinction is recorded rather
than glossed over: every ingestion carries `transport='REPLAY'`, so provenance
never implies an HTTP request that did not happen. The freshness service will
correctly report replayed data as stale, because it is.

Two things this makes possible:

* verifying the entire pipeline — parse, validate, quarantine, persist, serve —
  on real data shapes in an environment with no route to the exchange
* deterministic tests that exercise the production parsers rather than a
  simplified stand-in

`MockProvider` (fabricated values, forbidden in production) is a different thing
entirely and does not exist in this module.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.core.config import AppEnv, Settings, get_settings
from app.core.logging import get_logger
from app.models.ops import Transport
from app.providers import twse
from app.providers.base import (
    BaseMarketDataProvider,
    ProviderError,
    ProviderErrorKind,
    ProviderHealth,
    ProviderResult,
    SourceMetadata,
)
from app.providers.normalize import parse_roc_date

log = get_logger(__name__)

DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "twse"

# dataset -> (fixture filename, the endpoint the recording came from)
FIXTURES: dict[str, tuple[str, str]] = {
    "trading_calendar": (
        "holiday_schedule_2026.json",
        f"{twse.OPENAPI_BASE}/holidaySchedule/holidaySchedule",
    ),
    "stock_master": ("t187ap03_L.json", f"{twse.OPENAPI_BASE}/opendata/t187ap03_L"),
    "daily_prices_snapshot": (
        "stock_day_all_1150814.json",
        f"{twse.OPENAPI_BASE}/exchangeReport/STOCK_DAY_ALL",
    ),
    "daily_prices_history": (
        "stock_day_2330_202607.json",
        f"{twse.RWD_BASE}/afterTrading/STOCK_DAY",
    ),
    "index_quotes": ("mi_index_1150814.json", f"{twse.OPENAPI_BASE}/exchangeReport/MI_INDEX"),
    "institutional_flow": ("t86_20260814.json", f"{twse.RWD_BASE}/fund/T86"),
}

# The symbol and month the historical fixture actually covers.
REPLAY_HISTORY_SYMBOL = "2330"
REPLAY_HISTORY_MONTH = date(2026, 7, 1)


class ReplayProvider(BaseMarketDataProvider):
    code = "TWSE"  # the data really is TWSE data
    market = "TWSE"
    capabilities = twse.TWSEProvider.capabilities
    transport = Transport.REPLAY

    def __init__(
        self,
        fixture_dir: Path | None = None,
        settings: Settings | None = None,
        *,
        allow_in_production: bool = False,
    ) -> None:
        settings = settings or get_settings()
        if settings.APP_ENV is AppEnv.PRODUCTION and not allow_in_production:
            # Replayed data is real but old. Serving it from production without
            # an explicit decision would misrepresent freshness.
            raise RuntimeError(
                "ReplayProvider is disabled in production. Recorded responses are "
                "genuine but historical; enable deliberately if that is intended."
            )
        self.fixture_dir = fixture_dir or DEFAULT_FIXTURE_DIR
        if not self.fixture_dir.is_dir():
            raise RuntimeError(f"fixture directory not found: {self.fixture_dir}")

    # ------------------------------------------------------------------
    def _load(self, dataset: str) -> tuple[Any, SourceMetadata]:
        try:
            filename, endpoint = FIXTURES[dataset]
        except KeyError as exc:
            raise ProviderError(
                ProviderErrorKind.NOT_SUPPORTED,
                f"no recording for dataset '{dataset}'",
                source=self.code,
            ) from exc

        path = self.fixture_dir / filename
        if not path.is_file():
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"recording missing: {path}",
                source=self.code,
            )

        body = path.read_bytes()
        now = datetime.now(UTC)
        metadata = SourceMetadata(
            source=self.code,
            source_endpoint=endpoint,
            dataset=dataset,
            transport=Transport.REPLAY,
            source_request_at=now,
            source_response_at=now,
            params={"recording": filename},
            http_status=200,
            duration_ms=0,
            response_bytes=len(body),
            response_hash=hashlib.sha256(body).hexdigest(),
        )
        return json.loads(body), metadata

    # ------------------------------------------------------------------
    async def get_market_status(self) -> ProviderResult:
        payload, meta = self._load("index_quotes")
        meta.dataset = "market_status"
        records, errors = twse.parse_index_quotes(payload)
        as_of = records[0]["trading_date"] if records else None
        meta.data_as_of = as_of
        return ProviderResult(
            [{"market": self.market, "last_trading_date": as_of, "index_count": len(records)}],
            meta,
            errors,
            raw_payload=payload,
        )

    async def get_trading_calendar(self, year: int) -> ProviderResult:
        payload, meta = self._load("trading_calendar")
        records, errors = twse.parse_holiday_schedule(payload, year)
        # The recording covers 2026 only; asking for another year would silently
        # produce a calendar for the wrong one.
        if records and records[0]["calendar_date"].year != year:
            raise ProviderError(
                ProviderErrorKind.NO_DATA,
                f"recording covers {records[0]['calendar_date'].year}, not {year}",
                source=self.code,
            )
        return ProviderResult(records, meta, errors, raw_payload=payload)

    async def get_stock_master(self) -> ProviderResult:
        payload, meta = self._load("stock_master")
        records, errors = twse.parse_stock_master(payload)
        return ProviderResult(records, meta, errors, raw_payload=payload)

    async def get_daily_prices(
        self,
        *,
        trading_date: date | None = None,
        symbol: str | None = None,
        month: date | None = None,
    ) -> ProviderResult:
        if symbol is not None:
            if symbol != REPLAY_HISTORY_SYMBOL:
                raise ProviderError(
                    ProviderErrorKind.NO_DATA,
                    f"recording covers {REPLAY_HISTORY_SYMBOL} only, not {symbol}",
                    source=self.code,
                )
            target = month or trading_date
            if target is not None and (target.year, target.month) != (
                REPLAY_HISTORY_MONTH.year,
                REPLAY_HISTORY_MONTH.month,
            ):
                raise ProviderError(
                    ProviderErrorKind.NO_DATA,
                    f"recording covers {REPLAY_HISTORY_MONTH:%Y-%m}, not {target:%Y-%m}",
                    source=self.code,
                )
            payload, meta = self._load("daily_prices_history")
            meta.dataset = "daily_prices"
            records, errors = twse.parse_stock_day(payload, symbol)
            meta.data_as_of = records[-1]["trading_date"] if records else None
            return ProviderResult(records, meta, errors, raw_payload=payload)

        payload, meta = self._load("daily_prices_snapshot")
        meta.dataset = "daily_prices"
        records, errors = twse.parse_stock_day_all(payload)
        meta.data_as_of = records[0]["trading_date"] if records else None
        if trading_date is not None and meta.data_as_of != trading_date:
            raise ProviderError(
                ProviderErrorKind.NO_DATA,
                f"recording is for {meta.data_as_of}, not {trading_date}",
                source=self.code,
            )
        return ProviderResult(records, meta, errors, raw_payload=payload)

    async def get_market_index(self, *, trading_date: date | None = None) -> ProviderResult:
        payload, meta = self._load("index_quotes")
        records, errors = twse.parse_index_quotes(payload)
        meta.data_as_of = records[0]["trading_date"] if records else None
        if trading_date is not None and meta.data_as_of != trading_date:
            raise ProviderError(
                ProviderErrorKind.NO_DATA,
                f"recording is for {meta.data_as_of}, not {trading_date}",
                source=self.code,
            )
        return ProviderResult(records, meta, errors, raw_payload=payload)

    async def get_institutional_flow(self, *, trading_date: date) -> ProviderResult:
        payload, meta = self._load("institutional_flow")
        recorded = parse_roc_date(payload.get("date"), field="date")
        if recorded != trading_date:
            raise ProviderError(
                ProviderErrorKind.NO_DATA,
                f"recording is for {recorded}, not {trading_date}",
                source=self.code,
            )
        records, errors = twse.parse_institutional_flow(payload, trading_date)
        meta.data_as_of = trading_date
        return ProviderResult(records, meta, errors, raw_payload=payload)

    async def health(self) -> ProviderHealth:
        missing = [
            name for name, (fn, _) in FIXTURES.items() if not (self.fixture_dir / fn).is_file()
        ]
        return ProviderHealth(
            source=self.code,
            reachable=not missing,
            latency_ms=0.0,
            checked_at=datetime.now(UTC),
            error=f"missing recordings: {missing}" if missing else None,
            detail={
                "transport": str(Transport.REPLAY),
                "fixture_dir": str(self.fixture_dir),
                "datasets": sorted(FIXTURES),
                "note": "genuine TWSE responses recorded 2026-08-15; transport is replayed",
            },
        )


__all__ = ["FIXTURES", "REPLAY_HISTORY_MONTH", "REPLAY_HISTORY_SYMBOL", "ReplayProvider"]
