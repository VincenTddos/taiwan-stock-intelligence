"""Quant API: indicators, factors, rankings, screener.

Same rule as the market API — `Frontend → FastAPI → PostgreSQL`, never a
computation on the request path. Indicators and factor scores are produced by a
job and read here; a request that computed them would take a minute, would
depend on how much history happened to be loaded, and would let two users see
different numbers for the same day.

Two things this module does that the market API does not need to:

**Every response names its feature version.** A factor score is meaningless
without the definition that produced it, and definitions change. The version
travels in `meta.model_version` so a screenshot can be traced back to a spec.

**The screener refuses conditions it cannot evaluate.** Asking to filter on
`earnings_yield` when no financial statements have been ingested returns a 422
naming the missing factor rather than an empty result — an empty screener result
is indistinguishable from "no stock passed", which is the wrong answer.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Select, func, select

from app.api.deps import SessionDep
from app.core.errors import DataNotAvailableError, ValidationError
from app.core.logging import get_logger
from app.models.market import StockMaster
from app.models.quant import FactorScore, FactorValidityRecord, TechnicalIndicator
from app.quant import factors as fct
from app.quant import indicators as ind
from app.schemas.envelope import CacheMeta, Envelope, Meta, Pagination, envelope

log = get_logger(__name__)
router = APIRouter(tags=["quant"])

MAX_SCREENER_ROWS = 500


def _meta(*, trading_date: date | None, feature_version: str | None) -> Meta:
    """Provenance for a computed payload.

    `price_basis` deliberately travels in the payload rather than here: the
    shared envelope is the same shape for every endpoint in the platform, and
    widening it for one module's concern is how envelopes turn into grab bags.
    """
    return Meta(
        data_timestamp=datetime.now(),
        trading_date=trading_date,
        data_as_of=trading_date,
        source=["SELF"],
        is_demo=False,
        is_stale=False,
        model_version=feature_version,
        feature_version=feature_version,
        cache=CacheMeta(hit=False, age_seconds=None),
    )


async def _latest_indicator_date(session: SessionDep, symbol: str | None = None) -> date | None:
    query = select(func.max(TechnicalIndicator.trading_date))
    if symbol:
        query = query.where(TechnicalIndicator.symbol == symbol)
    return (await session.execute(query)).scalar_one_or_none()


async def _latest_factor_date(session: SessionDep) -> date | None:
    return (await session.execute(select(func.max(FactorScore.trading_date)))).scalar_one_or_none()


# ------------------------------------------------------------------ catalogue
@router.get("/quant/indicators/catalogue", response_model=Envelope[list[dict[str, Any]]])
async def indicator_catalogue() -> Envelope[list[dict[str, Any]]]:
    """Every indicator this build can compute, with its inputs and lookback.

    Served from the registry rather than from the database, so it describes the
    code that is running rather than whatever a job last happened to write.
    """
    payload = [
        {
            "name": spec.name,
            "category": spec.category,
            "description": spec.description,
            "inputs": list(spec.inputs),
            "lookback_sessions": spec.lookback,
            "params": spec.params,
        }
        for spec in sorted(ind.REGISTRY.values(), key=lambda s: (s.category, s.name))
    ]
    return envelope(payload, meta=_meta(trading_date=None, feature_version=None))


@router.get("/quant/factors/catalogue", response_model=Envelope[list[dict[str, Any]]])
async def factor_catalogue(session: SessionDep) -> Envelope[list[dict[str, Any]]]:
    """Every declared factor, its specification, and its latest verdict.

    Includes the factors that cannot be computed yet, each naming the input it
    is waiting for. A catalogue that listed only the working ones would make a
    missing data source look like a missing feature.
    """
    ranked = (
        select(
            FactorValidityRecord.factor_name,
            FactorValidityRecord.verdict,
            FactorValidityRecord.metrics,
            FactorValidityRecord.evaluated_from,
            FactorValidityRecord.evaluated_to,
            func.row_number()
            .over(
                partition_by=FactorValidityRecord.factor_name,
                order_by=FactorValidityRecord.evaluated_at.desc(),
            )
            .label("rn"),
        )
        .subquery()
        .alias("ranked")
    )
    latest = (await session.execute(select(ranked).where(ranked.c.rn == 1))).all()
    verdicts = {row[0]: row for row in latest}

    payload: list[dict[str, Any]] = []
    for spec in sorted(fct.REGISTRY, key=lambda s: (str(s.category), s.name)):
        record = verdicts.get(spec.name)
        payload.append(
            {
                **spec.as_dict(),
                "verdict": record[1] if record else None,
                "metrics": record[2] if record else None,
                "evaluated_from": record[3] if record else None,
                "evaluated_to": record[4] if record else None,
            }
        )
    return envelope(payload, meta=_meta(trading_date=None, feature_version=None))


# ----------------------------------------------------------------- indicators
@router.get("/stocks/{symbol}/indicators", response_model=Envelope[dict[str, Any]])
async def stock_indicators(
    symbol: str,
    session: SessionDep,
    trading_date: Annotated[date | None, Query(description="defaults to the latest")] = None,
    names: Annotated[str | None, Query(description="comma-separated subset")] = None,
) -> Envelope[dict[str, Any]]:
    query = select(TechnicalIndicator).where(TechnicalIndicator.symbol == symbol.upper())
    if trading_date:
        query = query.where(TechnicalIndicator.trading_date == trading_date)
    row = (
        await session.execute(query.order_by(TechnicalIndicator.trading_date.desc()).limit(1))
    ).scalar_one_or_none()

    if row is None:
        raise DataNotAvailableError(
            f"no indicators computed for {symbol}"
            + (f" on {trading_date}" if trading_date else "")
            + "; run the quant job for that session"
        )

    values = dict(row.indicators)
    if names:
        wanted = {n.strip() for n in names.split(",") if n.strip()}
        unknown = sorted(wanted - set(ind.REGISTRY))
        if unknown:
            raise ValidationError(f"unknown indicators: {unknown}")
        values = {k: v for k, v in values.items() if k in wanted}

    payload = {
        "symbol": row.symbol,
        "trading_date": row.trading_date,
        "price_basis": row.price_basis,
        "indicators": values,
    }
    return envelope(
        payload,
        meta=_meta(
            trading_date=row.trading_date,
            feature_version=row.feature_version,
        ),
    )


@router.get("/stocks/{symbol}/factors", response_model=Envelope[list[dict[str, Any]]])
async def stock_factors(
    symbol: str,
    session: SessionDep,
    trading_date: Annotated[date | None, Query()] = None,
) -> Envelope[list[dict[str, Any]]]:
    """One symbol's factor scores — the data behind a factor radar."""
    day = trading_date or await _latest_factor_date(session)
    if day is None:
        raise DataNotAvailableError("no factor scores have been computed yet")

    rows = (
        (
            await session.execute(
                select(FactorScore)
                .where(FactorScore.symbol == symbol.upper(), FactorScore.trading_date == day)
                .order_by(FactorScore.category, FactorScore.factor_name)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise DataNotAvailableError(f"no factor scores for {symbol} on {day}")

    payload = [
        {
            "factor": r.factor_name,
            "category": r.category,
            "raw_value": r.raw_value,
            "zscore": r.zscore,
            "percentile": r.percentile,
            "sector_zscore": r.sector_zscore,
            "score": r.score,
        }
        for r in rows
    ]
    return envelope(payload, meta=_meta(trading_date=day, feature_version=rows[0].feature_version))


@router.get("/factors/ranking", response_model=Envelope[list[dict[str, Any]]])
async def factor_ranking(
    session: SessionDep,
    factor: Annotated[str, Query(description="factor name")],
    trading_date: Annotated[date | None, Query()] = None,
    order: Annotated[str, Query(pattern="^(top|bottom)$")] = "top",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Envelope[list[dict[str, Any]]]:
    """Best or worst symbols on one factor.

    Ordered by `score`, which already has the factor's direction applied — so
    "top" means best rather than largest, and a low-volatility factor ranks the
    calm stocks first without the caller needing to know that.
    """
    if factor not in fct.REGISTRY.specs:
        raise ValidationError(f"unknown factor '{factor}'")

    day = trading_date or await _latest_factor_date(session)
    if day is None:
        raise DataNotAvailableError("no factor scores have been computed yet")

    ordering = FactorScore.score.desc() if order == "top" else FactorScore.score.asc()
    rows = (
        (
            await session.execute(
                select(FactorScore)
                .where(
                    FactorScore.factor_name == factor,
                    FactorScore.trading_date == day,
                    FactorScore.score.is_not(None),
                )
                .order_by(ordering)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise DataNotAvailableError(f"no scores for '{factor}' on {day}")

    names = await _symbol_names(session, [r.symbol for r in rows])
    payload = [
        {
            "rank": n + 1,
            "symbol": r.symbol,
            "name": names.get(r.symbol),
            "raw_value": r.raw_value,
            "score": r.score,
            "percentile": r.percentile,
        }
        for n, r in enumerate(rows)
    ]
    return envelope(payload, meta=_meta(trading_date=day, feature_version=rows[0].feature_version))


async def _symbol_names(session: SessionDep, symbols: list[str]) -> dict[str, str]:
    if not symbols:
        return {}
    rows = (
        await session.execute(
            select(StockMaster.symbol, StockMaster.name).where(
                StockMaster.symbol.in_(symbols), StockMaster.is_current.is_(True)
            )
        )
    ).all()
    return {r[0]: r[1] for r in rows}


# ------------------------------------------------------------------- screener
class ScreenerCondition(BaseModel):
    """One condition. Compared on the requested field, not always the score."""

    factor: str
    field: str = Field(default="score", pattern="^(score|zscore|percentile|raw_value)$")
    op: str = Field(pattern="^(gt|gte|lt|lte|between)$")
    value: float | None = None
    upper: float | None = None

    @field_validator("factor")
    @classmethod
    def known_factor(cls, value: str) -> str:
        if value not in fct.REGISTRY.specs:
            raise ValueError(f"unknown factor '{value}'")
        return value


class ScreenerRequest(BaseModel):
    conditions: list[ScreenerCondition] = Field(min_length=1, max_length=10)
    trading_date: date | None = None
    sort_by: str | None = None
    descending: bool = True
    limit: int = Field(default=100, ge=1, le=MAX_SCREENER_ROWS)


@router.post("/screener", response_model=Envelope[list[dict[str, Any]]])
async def screener(request: ScreenerRequest, session: SessionDep) -> Envelope[list[dict[str, Any]]]:
    """Symbols satisfying every condition, with the values that passed them.

    The passing values are returned, not just the symbols. A screener that
    answers "these forty stocks" without saying why produces a list nobody can
    check, and the first question anyone asks of a screen is which number put a
    given stock in it.
    """
    day = request.trading_date or await _latest_factor_date(session)
    if day is None:
        raise DataNotAvailableError("no factor scores have been computed yet")

    wanted = {c.factor for c in request.conditions}
    if request.sort_by:
        wanted.add(request.sort_by)

    available = set(
        (
            await session.execute(
                select(FactorScore.factor_name).where(FactorScore.trading_date == day).distinct()
            )
        )
        .scalars()
        .all()
    )
    missing = sorted(wanted - available)
    if missing:
        # Explicitly not an empty result. "Nothing passed" and "this factor was
        # never computed" are different answers and must not look alike.
        raise ValidationError(
            f"no scores on {day} for: {missing}. "
            f"{len(available)} factors are available for that session."
        )

    query: Select[Any] = select(
        FactorScore.symbol,
        FactorScore.factor_name,
        FactorScore.score,
        FactorScore.zscore,
        FactorScore.percentile,
        FactorScore.raw_value,
        FactorScore.feature_version,
    ).where(FactorScore.trading_date == day, FactorScore.factor_name.in_(wanted))
    rows = (await session.execute(query)).all()

    by_symbol: dict[str, dict[str, dict[str, Any]]] = {}
    version = None
    for symbol, name, score, zscore, percentile, raw_value, feature_version in rows:
        version = version or feature_version
        by_symbol.setdefault(symbol, {})[name] = {
            "score": score,
            "zscore": zscore,
            "percentile": percentile,
            "raw_value": raw_value,
        }

    passed: list[dict[str, Any]] = []
    for symbol, values in by_symbol.items():
        if not all(_satisfies(values.get(c.factor), c) for c in request.conditions):
            continue
        entry: dict[str, Any] = {"symbol": symbol, "values": {}}
        for factor_name, fields in values.items():
            entry["values"][factor_name] = fields
        passed.append(entry)

    sort_key = request.sort_by or request.conditions[0].factor
    passed.sort(
        key=lambda row: _sort_value(row["values"].get(sort_key)),
        reverse=request.descending,
    )
    limited = passed[: request.limit]

    names = await _symbol_names(session, [row["symbol"] for row in limited])
    for row in limited:
        row["name"] = names.get(row["symbol"])

    return envelope(
        limited,
        meta=_meta(trading_date=day, feature_version=version),
        pagination=Pagination(
            page=1,
            page_size=request.limit,
            total=len(passed),
            total_pages=max(1, -(-len(passed) // request.limit)),
        ),
    )


def _satisfies(values: dict[str, Any] | None, condition: ScreenerCondition) -> bool:
    if values is None:
        return False
    value = values.get(condition.field)
    if value is None:
        # A null factor is not a failed condition and not a passed one. It is
        # unknown, and unknown does not pass a screen.
        return False
    number = float(value)
    if condition.op == "gt":
        return condition.value is not None and number > condition.value
    if condition.op == "gte":
        return condition.value is not None and number >= condition.value
    if condition.op == "lt":
        return condition.value is not None and number < condition.value
    if condition.op == "lte":
        return condition.value is not None and number <= condition.value
    if condition.op == "between":
        return (
            condition.value is not None
            and condition.upper is not None
            and condition.value <= number <= condition.upper
        )
    return False


def _sort_value(values: dict[str, Any] | None) -> float:
    if values is None or values.get("score") is None:
        return float("-inf")
    return float(values["score"])


__all__ = ["router"]
