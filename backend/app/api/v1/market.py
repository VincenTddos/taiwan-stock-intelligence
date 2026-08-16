"""Market data API.

The rule this module obeys without exception:

    Frontend → FastAPI → PostgreSQL

Never `FastAPI → TWSE`. A provider is only ever called from an ingestion job. If
a request could reach the exchange, then response time would depend on the
exchange being up, a user could trigger rate-limit exhaustion, and the data a
user sees could differ from the data a backtest sees. All three are avoided by
serving exclusively from what has already been ingested and validated.

Every response carries the Phase 1 envelope, so `data_timestamp`, `source` and
the staleness flag travel with the numbers rather than being looked up
separately.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import RedisDep, SessionDep
from app.core.cache import versioned_key
from app.core.errors import DataNotAvailableError
from app.core.logging import get_logger
from app.models.market import (
    DailyPrice,
    IndexQuote,
    InstitutionalFlow,
    MarketStatus,
    StockMaster,
    TradingCalendar,
)
from app.models.ops import DataFreshness, DataQuarantine, DataSource, FreshnessStatus
from app.schemas.envelope import CacheMeta, Envelope, Meta, Pagination, envelope
from app.services.freshness_service import DataFreshnessService

log = get_logger(__name__)
router = APIRouter(tags=["market"])

# Per-endpoint TTLs, chosen from how fast the underlying data actually moves.
# End-of-day data does not change once published, so its TTL is generous.
TTL_STATUS = 60
TTL_MASTER = 300
TTL_PRICES = 300
TTL_INDICES = 120
TTL_FLOW = 300


async def _freshness_meta(session: SessionDep, dataset: str) -> tuple[bool, date | None, str]:
    """Whether a dataset is stale, and what the newest data it holds is."""
    row = await session.get(DataFreshness, dataset)
    if row is None:
        return True, None, str(FreshnessStatus.MISSING)
    return row.status != FreshnessStatus.FRESH, row.last_data_date, row.status


def _meta(
    *,
    trading_date: date | None,
    is_stale: bool,
    sources: list[str],
    cache_hit: bool = False,
    cache_age: int | None = None,
    data_timestamp: datetime | None = None,
) -> Meta:
    return Meta(
        data_timestamp=data_timestamp,
        trading_date=trading_date,
        data_as_of=trading_date,
        source=sources,
        is_demo=False,
        is_stale=is_stale,
        cache=CacheMeta(hit=cache_hit, age_seconds=cache_age),
    )


async def _cached(redis: RedisDep, namespace: str, key: str) -> tuple[Any, int | None]:
    try:
        full = await versioned_key(redis, namespace, key)
        raw = await redis.get(full)
        if raw is None:
            return None, None
        ttl = await redis.ttl(full)
        return json.loads(raw), max(0, int(ttl)) if ttl and ttl > 0 else None
    except Exception:
        return None, None


async def _store(redis: RedisDep, namespace: str, key: str, value: Any, ttl: int) -> None:
    try:
        full = await versioned_key(redis, namespace, key)
        await redis.setex(full, ttl, json.dumps(value, default=str))
    except Exception:
        log.debug("cache_write_failed", namespace=namespace, key=key)


# ---------------------------------------------------------------- status
@router.get("/market/status", response_model=Envelope[dict[str, Any]])
async def market_status(
    session: SessionDep, redis: RedisDep, market: str = "TWSE"
) -> Envelope[dict[str, Any]]:
    """Operational state of a market. Reads a precomputed row; never a provider."""
    cached, age = await _cached(redis, "market_status", market)
    if cached is not None:
        return envelope(
            cached,
            meta=_meta(
                trading_date=date.fromisoformat(cached["last_trading_date"])
                if cached.get("last_trading_date")
                else None,
                is_stale=cached.get("is_stale", False),
                sources=["TWSE"],
                cache_hit=True,
                cache_age=age,
            ),
        )

    row = await session.get(MarketStatus, market)
    if row is None:
        raise DataNotAvailableError(
            f"no status for market '{market}'. Market data has not been ingested yet."
        )

    stale, _last_data, freshness = await _freshness_meta(session, "daily_prices")
    payload = {
        "market": row.market,
        "last_trading_date": row.last_trading_date,
        "next_trading_date": row.next_trading_date,
        "is_trading_day_today": row.is_trading_day_today,
        "session_type_today": row.session_type_today,
        "symbol_count": row.symbol_count,
        "price_row_count": row.price_row_count,
        "coverage": {"from": row.earliest_price_date, "to": row.latest_price_date},
        "freshness": freshness,
        "is_stale": stale,
        "updated_at": row.updated_at,
    }
    await _store(redis, "market_status", market, payload, TTL_STATUS)
    return envelope(
        payload,
        meta=_meta(
            trading_date=row.last_trading_date,
            is_stale=stale,
            sources=["TWSE"],
            data_timestamp=row.updated_at,
        ),
    )


@router.get("/market/calendar", response_model=Envelope[list[dict[str, Any]]])
async def market_calendar(
    session: SessionDep,
    market: str = "TWSE",
    start: date | None = None,
    end: date | None = None,
    trading_only: bool = False,
) -> Envelope[list[dict[str, Any]]]:
    stmt = select(TradingCalendar).where(TradingCalendar.market == market)
    if start:
        stmt = stmt.where(TradingCalendar.calendar_date >= start)
    if end:
        stmt = stmt.where(TradingCalendar.calendar_date <= end)
    if trading_only:
        stmt = stmt.where(TradingCalendar.is_trading_day.is_(True))
    stmt = stmt.order_by(TradingCalendar.calendar_date).limit(800)

    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        raise DataNotAvailableError(
            f"trading calendar for {market} is not populated for that range"
        )
    return envelope(
        [
            {
                "date": r.calendar_date,
                "is_trading_day": r.is_trading_day,
                "session_type": r.session_type,
                "holiday_name": r.holiday_name,
                "verified_by_volume": r.verified_by_volume,
            }
            for r in rows
        ],
        meta=_meta(trading_date=None, is_stale=False, sources=[rows[0].source]),
    )


# ---------------------------------------------------------------- stocks
@router.get("/stocks", response_model=Envelope[list[dict[str, Any]]])
async def list_stocks(
    session: SessionDep,
    market: str = "TWSE",
    q: Annotated[str | None, Query(max_length=50)] = None,
    industry: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Envelope[list[dict[str, Any]]]:
    base = select(StockMaster).where(StockMaster.market == market, StockMaster.is_current.is_(True))
    if q:
        pattern = f"%{q}%"
        base = base.where(
            StockMaster.symbol.ilike(pattern)
            | StockMaster.name.ilike(pattern)
            | StockMaster.short_name.ilike(pattern)
            | StockMaster.short_name_en.ilike(pattern)
        )
    if industry:
        base = base.where(StockMaster.industry_code == industry)

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(StockMaster.symbol).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    stale, _, _ = await _freshness_meta(session, "stock_master")
    result = envelope(
        [
            {
                "symbol": r.symbol,
                "name": r.name,
                "short_name": r.short_name,
                "short_name_en": r.short_name_en,
                "market": r.market,
                "industry_code": r.industry_code,
                "industry_name": r.industry_name,
                "listing_date": r.listing_date,
                "status": r.status,
            }
            for r in rows
        ],
        meta=_meta(trading_date=None, is_stale=stale, sources=["TWSE"]),
    )
    result.pagination = Pagination(
        page=page,
        page_size=page_size,
        total=int(total),
        total_pages=max(1, -(-int(total) // page_size)),
    )
    return result


@router.get("/stocks/{symbol}", response_model=Envelope[dict[str, Any]])
async def get_stock(
    session: SessionDep, redis: RedisDep, symbol: str, market: str = "TWSE"
) -> Envelope[dict[str, Any]]:
    symbol = symbol.upper()
    cached, age = await _cached(redis, "stock_master", f"{market}:{symbol}")
    if cached is not None:
        return envelope(
            cached,
            meta=_meta(
                trading_date=None,
                is_stale=False,
                sources=["TWSE"],
                cache_hit=True,
                cache_age=age,
            ),
        )

    row = (
        await session.execute(
            select(StockMaster).where(
                StockMaster.symbol == symbol,
                StockMaster.market == market,
                StockMaster.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise DataNotAvailableError(f"no master record for {symbol} in {market}")

    latest = (
        await session.execute(
            select(DailyPrice)
            .where(DailyPrice.symbol == symbol, DailyPrice.market == market)
            .order_by(DailyPrice.trading_date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    payload = {
        "symbol": row.symbol,
        "name": row.name,
        "short_name": row.short_name,
        "short_name_en": row.short_name_en,
        "market": row.market,
        "industry_code": row.industry_code,
        "industry_name": row.industry_name,
        "listing_date": row.listing_date,
        "delisting_date": row.delisting_date,
        "status": row.status,
        "par_value": row.par_value,
        "shares_outstanding": row.shares_outstanding,
        "latest_price": (
            {
                "trading_date": latest.trading_date,
                "open": latest.open,
                "high": latest.high,
                "low": latest.low,
                "close": latest.close,
                "change": latest.change,
                "volume": latest.volume,
                "turnover": latest.turnover,
                "quality_status": latest.quality_status,
            }
            if latest
            else None
        ),
    }
    await _store(redis, "stock_master", f"{market}:{symbol}", payload, TTL_MASTER)
    stale, _, _ = await _freshness_meta(session, "daily_prices")
    return envelope(
        payload,
        meta=_meta(
            trading_date=latest.trading_date if latest else None,
            is_stale=stale,
            sources=["TWSE"],
        ),
    )


@router.get("/stocks/{symbol}/prices", response_model=Envelope[dict[str, Any]])
async def get_prices(
    session: SessionDep,
    redis: RedisDep,
    symbol: str,
    market: str = "TWSE",
    start: date | None = None,
    end: date | None = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 250,
    include_suspect: bool = False,
) -> Envelope[dict[str, Any]]:
    """Daily OHLCV. Raw prices as published; `adjusted_close` is separate."""
    symbol = symbol.upper()
    cache_key = f"{market}:{symbol}:{start}:{end}:{limit}:{include_suspect}"
    cached, age = await _cached(redis, "prices", cache_key)
    if cached is not None:
        return envelope(
            cached,
            meta=_meta(
                trading_date=date.fromisoformat(cached["last_trading_date"])
                if cached.get("last_trading_date")
                else None,
                is_stale=cached.get("is_stale", False),
                sources=["TWSE"],
                cache_hit=True,
                cache_age=age,
            ),
        )

    stmt = select(DailyPrice).where(DailyPrice.symbol == symbol, DailyPrice.market == market)
    if start:
        stmt = stmt.where(DailyPrice.trading_date >= start)
    if end:
        stmt = stmt.where(DailyPrice.trading_date <= end)
    if not include_suspect:
        stmt = stmt.where(DailyPrice.quality_status == "OK")

    rows = list(
        (await session.execute(stmt.order_by(DailyPrice.trading_date.desc()).limit(limit)))
        .scalars()
        .all()
    )
    if not rows:
        # No data is an answer. We do not fabricate a series.
        raise DataNotAvailableError(
            f"no price data for {symbol} in {market} for the requested range"
        )
    rows.reverse()

    stale, _, _ = await _freshness_meta(session, "daily_prices")
    payload = {
        "symbol": symbol,
        "market": market,
        "adjusted": False,
        "count": len(rows),
        "last_trading_date": rows[-1].trading_date,
        "is_stale": stale,
        "bars": [
            {
                "trading_date": r.trading_date,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "change": r.change,
                "volume": r.volume,
                "turnover": r.turnover,
                "trade_count": r.trade_count,
                "adjusted_close": r.adjusted_close,
                "quality_status": r.quality_status,
                "quality_flags": r.quality_flags,
                "note": r.note,
            }
            for r in rows
        ],
    }
    await _store(redis, "prices", cache_key, payload, TTL_PRICES)
    return envelope(
        payload,
        meta=_meta(
            trading_date=rows[-1].trading_date,
            is_stale=stale,
            sources=["TWSE"],
        ),
    )


# --------------------------------------------------------------- indices
@router.get("/indices", response_model=Envelope[list[dict[str, Any]]])
async def list_indices(
    session: SessionDep,
    redis: RedisDep,
    market: str = "TWSE",
    trading_date: date | None = None,
    index_type: str | None = None,
) -> Envelope[list[dict[str, Any]]]:
    if trading_date is None:
        trading_date = (
            await session.execute(
                select(func.max(IndexQuote.trading_date)).where(IndexQuote.market == market)
            )
        ).scalar_one_or_none()
    if trading_date is None:
        raise DataNotAvailableError("no index data has been ingested")

    cache_key = f"{market}:{trading_date}:{index_type}"
    cached, age = await _cached(redis, "indices", cache_key)
    if cached is not None:
        return envelope(
            cached,
            meta=_meta(
                trading_date=trading_date,
                is_stale=False,
                sources=["TWSE"],
                cache_hit=True,
                cache_age=age,
            ),
        )

    stmt = select(IndexQuote).where(
        IndexQuote.market == market, IndexQuote.trading_date == trading_date
    )
    if index_type:
        stmt = stmt.where(IndexQuote.index_type == index_type)

    rows = (
        (await session.execute(stmt.order_by(IndexQuote.index_type, IndexQuote.index_code)))
        .scalars()
        .all()
    )
    if not rows:
        raise DataNotAvailableError(f"no index data for {trading_date}")

    stale, _, _ = await _freshness_meta(session, "index_quotes")
    payload = [
        {
            "index_code": r.index_code,
            "index_name": r.index_name,
            "index_type": r.index_type,
            "trading_date": r.trading_date,
            "close": r.close,
            "change": r.change,
            "change_pct": r.change_pct,
        }
        for r in rows
    ]
    await _store(redis, "indices", cache_key, payload, TTL_INDICES)
    return envelope(
        payload,
        meta=_meta(
            trading_date=trading_date,
            is_stale=stale,
            sources=["TWSE"],
        ),
    )


# --------------------------------------------------------- institutional
@router.get("/institutional", response_model=Envelope[list[dict[str, Any]]])
async def institutional(
    session: SessionDep,
    market: str = "TWSE",
    symbol: str | None = None,
    trading_date: date | None = None,
    investor_type: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Envelope[list[dict[str, Any]]]:
    """Institutional flow. Buy, sell and net are all returned — not just net."""
    if trading_date is None:
        trading_date = (
            await session.execute(
                select(func.max(InstitutionalFlow.trading_date)).where(
                    InstitutionalFlow.market == market
                )
            )
        ).scalar_one_or_none()
    if trading_date is None:
        raise DataNotAvailableError("no institutional flow data has been ingested")

    base = select(InstitutionalFlow).where(
        InstitutionalFlow.market == market, InstitutionalFlow.trading_date == trading_date
    )
    if symbol:
        base = base.where(InstitutionalFlow.symbol == symbol.upper())
    if investor_type:
        base = base.where(InstitutionalFlow.investor_type == investor_type.upper())

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(InstitutionalFlow.symbol, InstitutionalFlow.investor_type)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    stale, _, _ = await _freshness_meta(session, "institutional_flow")
    result = envelope(
        [
            {
                "symbol": r.symbol,
                "trading_date": r.trading_date,
                "investor_type": r.investor_type,
                "buy_volume": r.buy_volume,
                "sell_volume": r.sell_volume,
                "net_volume": r.net_volume,
                "net_value": r.net_value,
            }
            for r in rows
        ],
        meta=_meta(trading_date=trading_date, is_stale=stale, sources=["TWSE"]),
    )
    result.pagination = Pagination(
        page=page,
        page_size=page_size,
        total=int(total),
        total_pages=max(1, -(-int(total) // page_size)),
    )
    return result


# ------------------------------------------------------------ operations
@router.get("/market/data-operations", response_model=Envelope[dict[str, Any]])
async def data_operations(session: SessionDep) -> Envelope[dict[str, Any]]:
    """Everything the Data Operations dashboard needs, in one call."""
    freshness = await DataFreshnessService(session).current()
    sources = (await session.execute(select(DataSource).order_by(DataSource.code))).scalars().all()

    quarantine_rows = (
        await session.execute(
            select(DataQuarantine.dataset, func.count())
            .where(DataQuarantine.reviewed_at.is_(None))
            .group_by(DataQuarantine.dataset)
        )
    ).all()
    quarantine = {ds: int(n) for ds, n in quarantine_rows}

    payload = {
        "overall": DataFreshnessService.overall(list(freshness)),
        "datasets": [
            {
                "dataset": f.dataset,
                "description": f.description,
                "status": f.status,
                "last_data_date": f.last_data_date,
                "last_ingested_at": f.last_ingested_at,
                "expected_next_update": f.expected_next_update,
                "record_count": f.record_count,
                "lag_minutes": f.lag_minutes,
                "expected_lag_minutes": f.expected_lag_minutes,
                "quarantined": quarantine.get(f.dataset, 0),
                "detail": f.detail,
            }
            for f in freshness
        ],
        "sources": [
            {
                "code": s.code,
                "name": s.name,
                "status": s.status,
                "market": s.market,
                "base_url": s.base_url,
                "verified_at": s.verified_at,
                "last_success_at": s.last_success_at,
                "last_failure_at": s.last_failure_at,
                "consecutive_failures": s.consecutive_failures,
                "last_error": s.last_error,
                "rate_limit_per_minute": s.rate_limit_per_minute,
                "notes": s.notes,
            }
            for s in sources
        ],
        "quarantine_total": sum(quarantine.values()),
    }
    return envelope(payload, meta=_meta(trading_date=None, is_stale=False, sources=["SELF"]))


__all__ = ["router"]
