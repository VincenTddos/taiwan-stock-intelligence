"""Ingestion jobs.

Each function is one dataset, end to end: acquire a provider, fetch, run the
pipeline, refresh derived state. They are plain async functions so they can be
called from Celery, from a CLI, or from a test without a broker.

Every job is idempotent. Running `ingest_daily_prices` twice for the same
session updates the same rows rather than duplicating them — which is what makes
a retry safe and a backfill restartable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.ingest.pipeline import IngestionOutcome, IngestionPipeline
from app.models.market import DailyPrice, MarketStatus, StockMaster
from app.models.ops import DataSource
from app.providers.base import BaseMarketDataProvider
from app.providers.corporate_actions import CorporateActionProvider
from app.providers.registry import ProviderRegistry
from app.services.calendar_service import TradingCalendarService
from app.services.corporate_action_service import CorporateActionCoverageService
from app.services.freshness_service import DataFreshnessService

log = get_logger(__name__)


async def _provider(
    session: AsyncSession, registry: ProviderRegistry, code: str = "TWSE"
) -> BaseMarketDataProvider:
    return await registry.get(session, code)


async def _mark_source(session: AsyncSession, code: str, *, ok: bool, error: str = "") -> None:
    row = (
        await session.execute(select(DataSource).where(DataSource.code == code))
    ).scalar_one_or_none()
    if row is None:
        return
    now = datetime.now(UTC)
    if ok:
        row.last_success_at = now
        row.consecutive_failures = 0
        row.last_error = None
    else:
        row.last_failure_at = now
        row.consecutive_failures += 1
        row.last_error = error[:500]


# ---------------------------------------------------------------- calendar
async def ingest_trading_calendar(
    session: AsyncSession, registry: ProviderRegistry, *, year: int, source: str = "TWSE"
) -> dict[str, Any]:
    """Build a full calendar year from the published closure schedule.

    Must run before any market data ingestion for that year — the pipeline
    quarantines records whose trading date has no calendar entry rather than
    guessing whether the market was open.
    """
    provider = await _provider(session, registry, source)
    calendar = TradingCalendarService(session)
    try:
        result = await provider.get_trading_calendar(year)
        await _mark_source(session, source, ok=True)
    except Exception as exc:
        await _mark_source(session, source, ok=False, error=str(exc))
        raise

    pipeline = IngestionPipeline(session)
    ingestion = await pipeline.record_ingestion(result)

    stats = await calendar.build_year(year, result.records, source=source)
    ingestion.accepted_count = stats["total"]
    await session.flush()

    return {
        "year": year,
        "ingestion_id": ingestion.id,
        "closures_published": len(result.records),
        "annotation_rows_ignored": len(result.parse_errors),
        **stats,
    }


# ------------------------------------------------------------ stock master
async def ingest_stock_master(
    session: AsyncSession, registry: ProviderRegistry, *, source: str = "TWSE"
) -> dict[str, Any]:
    """Refresh the security master as a slowly-changing dimension.

    A changed attribute closes the current row and opens a new one, so the
    history of names, industries and listing status is preserved. Overwriting in
    place would make it impossible to ask what a company was called in 2019.
    """
    provider = await _provider(session, registry, source)
    try:
        result = await provider.get_stock_master()
        await _mark_source(session, source, ok=True)
    except Exception as exc:
        await _mark_source(session, source, ok=False, error=str(exc))
        raise

    pipeline = IngestionPipeline(session)
    ingestion = await pipeline.record_ingestion(result)
    today = datetime.now(UTC).date()

    existing = {
        row.symbol: row
        for row in (
            await session.execute(
                select(StockMaster).where(
                    StockMaster.market == "TWSE", StockMaster.is_current.is_(True)
                )
            )
        ).scalars()
    }

    tracked = (
        "name",
        "short_name",
        "short_name_en",
        "industry_code",
        "industry_name",
        "status",
        "listing_date",
        "shares_outstanding",
    )
    inserted = updated = unchanged = quarantined = 0

    for rec in result.records:
        vr = pipeline.quality.validate_stock_master(rec)
        if not vr.accepted:
            await pipeline.quarantine(
                dataset="stock_master",
                source=result.metadata.source,
                raw_record=rec,
                rule_ids=[v.rule_id for v in vr.fatal],
                errors=[v.as_dict() for v in vr.violations],
                symbol=rec.get("symbol"),
                ingestion_id=ingestion.id,
            )
            quarantined += 1
            continue

        current = existing.get(rec["symbol"])
        if current is None:
            # `valid_from` is KNOWLEDGE time — when this version of the record
            # became known to us. It is not `listing_date`, which is event time
            # and may be decades earlier. Conflating them would make an SCD2
            # lookup for 1990 return a row we only learned in 2026.
            session.add(
                StockMaster(**rec, valid_from=today, is_current=True, ingestion_id=ingestion.id)
            )
            inserted += 1
            continue

        if all(getattr(current, f) == rec.get(f) for f in tracked):
            unchanged += 1
            continue

        # Close the old row and open a new one — SCD2.
        current.valid_to = today
        current.is_current = False
        session.add(
            StockMaster(**rec, valid_from=today, is_current=True, ingestion_id=ingestion.id)
        )
        updated += 1

    ingestion.accepted_count = inserted + updated
    ingestion.quarantined_count = quarantined
    await session.flush()

    log.info(
        "stock_master_ingested",
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        quarantined=quarantined,
    )
    return {
        "ingestion_id": ingestion.id,
        "records_in": result.record_count,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "quarantined": quarantined,
    }


# ------------------------------------------------------------ daily prices
async def _prev_closes(session: AsyncSession, trading_date: date) -> dict[str, Any]:
    """Previous close per symbol, so the validator can judge price moves."""
    calendar = TradingCalendarService(session)
    prev = await calendar.previous_trading_day(trading_date)
    if prev is None:
        return {}
    rows = await session.execute(
        select(DailyPrice.symbol, DailyPrice.close).where(DailyPrice.trading_date == prev)
    )
    return {sym: close for sym, close in rows.all() if close is not None}


async def ingest_daily_prices(
    session: AsyncSession,
    registry: ProviderRegistry,
    *,
    trading_date: date | None = None,
    symbol: str | None = None,
    month: date | None = None,
    source: str = "TWSE",
) -> IngestionOutcome:
    provider = await _provider(session, registry, source)
    try:
        result = await provider.get_daily_prices(
            trading_date=trading_date, symbol=symbol, month=month
        )
        await _mark_source(session, source, ok=True)
    except Exception as exc:
        await _mark_source(session, source, ok=False, error=str(exc))
        raise

    as_of = result.metadata.data_as_of
    prev = await _prev_closes(session, as_of) if as_of else {}

    pipeline = IngestionPipeline(session)
    return await pipeline.run(result, dataset="daily_prices", prev_closes=prev)


async def ingest_index_quotes(
    session: AsyncSession,
    registry: ProviderRegistry,
    *,
    trading_date: date | None = None,
    source: str = "TWSE",
) -> IngestionOutcome:
    provider = await _provider(session, registry, source)
    try:
        result = await provider.get_market_index(trading_date=trading_date)
        await _mark_source(session, source, ok=True)
    except Exception as exc:
        await _mark_source(session, source, ok=False, error=str(exc))
        raise
    return await IngestionPipeline(session).run(result, dataset="index_quotes")


async def ingest_institutional_flow(
    session: AsyncSession,
    registry: ProviderRegistry,
    *,
    trading_date: date,
    source: str = "TWSE",
) -> IngestionOutcome:
    provider = await _provider(session, registry, source)
    try:
        result = await provider.get_institutional_flow(trading_date=trading_date)
        await _mark_source(session, source, ok=True)
    except Exception as exc:
        await _mark_source(session, source, ok=False, error=str(exc))
        raise
    return await IngestionPipeline(session).run(result, dataset="institutional_flow")


# -------------------------------------------------------- corporate actions
def _coverage_window(
    provider: CorporateActionProvider, start: date, end: date, today: date
) -> tuple[date, date] | None:
    """The sub-range this source may honestly claim to have searched.

    Not the range that was requested. A forward-looking table asked for 2019
    answers, cheerfully, with nothing — and recording that as coverage of 2019
    would convert "this endpoint cannot see the past" into "no dividends were
    paid", which is the exact substitution the coverage table exists to prevent.

    `history_starts = None` means the reach into the past is unknown, and
    unknown is not unlimited: such a source may claim today forward and no
    further back.
    """
    lower = provider.history_starts or today
    lower = max(lower, start)
    return (lower, end) if lower <= end else None


async def ingest_corporate_actions(
    session: AsyncSession,
    registry: ProviderRegistry,
    *,
    start: date,
    end: date,
    source: str,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Ingest one source's corporate actions and record what it searched.

    The write of the actions and the write of the coverage are one unit. Actions
    without coverage cannot be adjusted with (`assert_adjustable` refuses), and
    coverage without actions is a false claim — so the interesting failure is
    the second, and it is avoided by deriving coverage from the same call that
    produced the actions rather than from a separate pass.
    """
    provider = await registry.get_corporate_actions(session, source)
    window = _coverage_window(provider, start, end, datetime.now(UTC).date())
    pipeline = IngestionPipeline(session)
    coverage = CorporateActionCoverageService(session)

    found: dict[str, int] = {}
    outcomes: list[IngestionOutcome] = []

    for chunk_start, chunk_end in _chunks(
        window[0] if window else start, window[1] if window else end, provider.max_query_days
    ):
        try:
            result = await provider.get_corporate_actions(
                start=chunk_start, end=chunk_end, symbol=symbol
            )
            await _mark_source(session, source, ok=True)
        except Exception as exc:
            await _mark_source(session, source, ok=False, error=str(exc))
            raise

        # The natural key is (symbol, action_type, ex_date, source), and `source`
        # here is the provider — not the exchange. Three TWSE endpoints see
        # different action types; collapsing them to 'TWSE' would make the
        # forecast table and the results table overwrite each other's rows.
        for rec in result.records:
            rec["source"] = provider.code
            found[rec["symbol"]] = found.get(rec["symbol"], 0) + 1

        # Ex-dates are trading days by construction, but the forecast table runs
        # ahead of the published calendar, and a calendar that has not been built
        # that far forward is not evidence that the market is shut.
        outcomes.append(
            await pipeline.run(result, dataset="corporate_actions", enforce_calendar=False)
        )

    outcome = _merge(outcomes, source=provider.code)

    covered_symbols = 0
    if window is not None:
        # Every symbol the endpoint searched, not only the ones it found
        # something for. A symbol that paid no dividend and a symbol nobody
        # looked at are the same empty set in `corporate_actions`; this is where
        # they stop being the same.
        universe = [symbol] if symbol else await _known_symbols(session, provider.market)
        for sym in universe:
            await coverage.record(
                symbol=sym,
                market=provider.market,
                source=provider.code,
                covered_from=window[0],
                covered_to=window[1],
                action_types=provider.supported_actions,
                actions_found=found.get(sym, 0),
                ingestion_id=outcome.ingestion_id,
            )
        covered_symbols = len(universe)
    await session.flush()

    log.info(
        "corporate_actions_ingested",
        source=provider.code,
        requested=f"{start}→{end}",
        covered=f"{window[0]}→{window[1]}" if window else None,
        records=outcome.records_written,
        quarantined=outcome.records_quarantined,
        symbols=covered_symbols,
    )
    return {
        "source": provider.code,
        "requested": [start, end],
        "covered": list(window) if window else None,
        "coverage_symbols": covered_symbols,
        "symbols_with_actions": len(found),
        "supported_actions": sorted(str(a) for a in provider.supported_actions),
        "outcome": outcome.as_dict(),
    }


def _chunks(start: date, end: date, max_days: int | None) -> list[tuple[date, date]]:
    """Split a range into spans the source will actually answer."""
    if max_days is None or (end - start).days < max_days:
        return [(start, end)]
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=max_days - 1), end)
        out.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return out


def _merge(outcomes: list[IngestionOutcome], *, source: str) -> IngestionOutcome:
    """One outcome for a job that may have taken several requests.

    Each chunk still writes its own `raw_ingestions` row, so provenance stays
    one row per call; this is only the summary the caller is handed.
    """
    total = IngestionOutcome(dataset="corporate_actions", source=source)
    for o in outcomes:
        total.records_in += o.records_in
        total.records_written += o.records_written
        total.records_quarantined += o.records_quarantined
        total.records_suspect += o.records_suspect
        total.parse_errors += o.parse_errors
        total.data_as_of = o.data_as_of or total.data_as_of
        total.ingestion_id = o.ingestion_id or total.ingestion_id
    total.timings_ms["requests"] = len(outcomes)
    return total


async def _known_symbols(session: AsyncSession, market: str) -> list[str]:
    """Every symbol the master has ever carried, current or not.

    Not just the currently listed ones. A full-market endpoint searched the
    delisted symbols too, and they are precisely the ones a backtest must be
    able to adjust — a universe restricted to survivors is where survivorship
    bias comes from.
    """
    rows = await session.execute(
        select(StockMaster.symbol)
        .where(StockMaster.market == market)
        .distinct()
        .order_by(StockMaster.symbol)
    )
    return [r[0] for r in rows.all()]


# ------------------------------------------------------------ derived state
async def refresh_market_status(session: AsyncSession, market: str = "TWSE") -> dict[str, Any]:
    """Materialise the snapshot `/market/status` serves.

    Precomputed so the endpoint is a single primary-key read and never touches a
    provider.
    """
    calendar = TradingCalendarService(session)
    today = datetime.now(UTC).date()

    stats = (
        await session.execute(
            select(
                func.count(),
                func.count(func.distinct(DailyPrice.symbol)),
                func.min(DailyPrice.trading_date),
                func.max(DailyPrice.trading_date),
            ).where(DailyPrice.market == market)
        )
    ).one()
    rows, symbols, first, last = stats

    cal_today = await calendar.get(today, market)
    payload = {
        "market": market,
        "last_trading_date": last,
        "next_trading_date": await calendar.next_trading_day(today, market),
        "is_trading_day_today": cal_today.is_trading_day if cal_today else None,
        "session_type_today": cal_today.session_type if cal_today else None,
        "symbol_count": int(symbols or 0),
        "price_row_count": int(rows or 0),
        "earliest_price_date": first,
        "latest_price_date": last,
        "updated_at": datetime.now(UTC),
    }

    stmt = pg_insert(MarketStatus).values(payload)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[MarketStatus.market],
            set_={k: getattr(stmt.excluded, k) for k in payload if k != "market"},
        )
    )
    await session.flush()
    return payload


async def refresh_freshness(session: AsyncSession, market: str = "TWSE") -> list[dict[str, Any]]:
    return await DataFreshnessService(session).evaluate_all(market=market)


__all__ = [
    "ingest_corporate_actions",
    "ingest_daily_prices",
    "ingest_index_quotes",
    "ingest_institutional_flow",
    "ingest_stock_master",
    "ingest_trading_calendar",
    "refresh_freshness",
    "refresh_market_status",
]
