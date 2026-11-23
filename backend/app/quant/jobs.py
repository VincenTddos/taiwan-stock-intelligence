"""Quant jobs: load, compute, persist.

This is the only module in `app.quant` that holds a session. Everything it calls
is a pure function over a frame, which is what makes the look-ahead test
meaningful — a factor that could reach the database could not be given a
poisoned panel.

The loader is where the two guards live.

**History is loaded by declared lookback, never by a constant.** `MAX_LOOKBACK`
comes from the registry, so adding `ma_480` extends the window rather than
producing a column of nulls that looks like missing data.

**The price basis is established before anything is computed, not asserted
afterwards.** Adjusted prices are used when the corporate action coverage guard
permits them; otherwise the panel is raw and every row written says so.
Computing on raw prices is a real, documented compromise while no source
publishes decomposable Taiwanese stock dividends before today — silently
computing on raw prices while the schema says `ADJUSTED` would not be.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.market import DailyPrice, IndexQuote, StockMaster
from app.models.quant import (
    FactorScore,
    FactorValidityRecord,
    FeatureVersion,
    TechnicalIndicator,
)
from app.quant import factors as fct
from app.quant import indicators as ind
from app.quant import panel as panel_mod
from app.quant.factors.validation import Verdict
from app.services.corporate_action_service import (
    AdjustmentNotPermitted,
    CorporateActionCoverageService,
)

log = get_logger(__name__)

FEATURE_SET = "quant.v1"
BENCHMARK_INDEX = "TAIEX"

#: Calendar days per trading day, used only to turn a lookback in sessions into
#: a date range wide enough to contain it. Deliberately generous: loading a few
#: extra weeks costs a little memory, while loading too few silently shortens
#: every window.
CALENDAR_SLACK = 1.6


@dataclass(slots=True)
class QuantRunReport:
    """What one computation did, and what it could not do."""

    trading_date: date
    feature_version: str
    price_basis: str
    symbols: int = 0
    indicator_rows: int = 0
    factor_rows: int = 0
    indicators_computed: int = 0
    factors_computed: int = 0
    factors_not_computable: dict[str, list[str]] = field(default_factory=dict)
    indicators_unavailable: dict[str, list[str]] = field(default_factory=dict)
    adjustment_blocked: str | None = None
    timings_ms: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date.isoformat(),
            "feature_version": self.feature_version,
            "price_basis": self.price_basis,
            "symbols": self.symbols,
            "indicator_rows": self.indicator_rows,
            "factor_rows": self.factor_rows,
            "indicators_computed": self.indicators_computed,
            "factors_computed": self.factors_computed,
            "factors_not_computable": self.factors_not_computable,
            "indicators_unavailable": self.indicators_unavailable,
            "adjustment_blocked": self.adjustment_blocked,
            "timings_ms": self.timings_ms,
        }


# --------------------------------------------------------------- versioning
def _code_version() -> str:
    """The git sha the computation ran from, or `unknown` outside a checkout.

    Stored beside the spec because the spec cannot hold the code. Together they
    say what was intended and what actually ran; either alone is half a record.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def spec_document() -> dict[str, Any]:
    """The full, serialisable definition of this feature set."""
    return {
        "feature_set": FEATURE_SET,
        "benchmark": BENCHMARK_INDEX,
        "indicators": [
            {
                "name": spec.name,
                "category": spec.category,
                "inputs": list(spec.inputs),
                "lookback": spec.lookback,
                "description": spec.description,
                "params": dict(spec.params),
            }
            for spec in ind.REGISTRY.values()
        ],
        "factors": [spec.as_dict() for spec in fct.REGISTRY],
        "thresholds": fct.validation.THRESHOLDS,
    }


def version_string(document: dict[str, Any] | None = None) -> str:
    """A version derived from the spec itself.

    Content-addressed rather than sequential: two deployments that computed the
    same definitions get the same version, and any change to a window, a
    threshold or a direction gets a different one automatically. A hand-bumped
    number is a number someone forgets to bump on the one change that mattered.
    """
    document = document or spec_document()
    payload = json.dumps(document, sort_keys=True, default=str).encode()
    digest = hashlib.sha256(payload).hexdigest()[:10]
    return f"{FEATURE_SET}-{digest}"


async def register_feature_version(session: AsyncSession) -> str:
    """Record this feature set, and return its version. Idempotent."""
    document = spec_document()
    version = version_string(document)
    stmt = pg_insert(FeatureVersion).values(
        version=version,
        feature_set=FEATURE_SET,
        spec=document,
        code_version=_code_version(),
        description=(
            f"{len(document['indicators'])} indicators, {len(document['factors'])} "
            f"factors; benchmark {BENCHMARK_INDEX}"
        ),
    )
    await session.execute(stmt.on_conflict_do_nothing(index_elements=[FeatureVersion.version]))
    await session.flush()
    return version


# ------------------------------------------------------------------ loading
async def _benchmark_frame(
    session: AsyncSession, start: date, end: date, index_code: str
) -> pl.DataFrame:
    rows = (
        await session.execute(
            select(IndexQuote.trading_date, IndexQuote.close)
            .where(
                IndexQuote.index_code == index_code,
                IndexQuote.trading_date >= start,
                IndexQuote.trading_date <= end,
                IndexQuote.close.is_not(None),
            )
            .order_by(IndexQuote.trading_date)
        )
    ).all()
    return pl.DataFrame(
        {
            "trading_date": [r[0] for r in rows],
            "benchmark_close": [float(r[1]) for r in rows],
        },
        schema={"trading_date": pl.Date, "benchmark_close": pl.Float64},
    )


async def _master_frame(session: AsyncSession, market: str) -> pl.DataFrame:
    rows = (
        await session.execute(
            select(
                StockMaster.symbol,
                StockMaster.sector,
                StockMaster.industry_name,
                StockMaster.shares_outstanding,
            ).where(StockMaster.market == market, StockMaster.is_current.is_(True))
        )
    ).all()
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            # Industry is the finer partition and is what TWSE actually
            # publishes; `sector` is often null. Neutralising against a column
            # that is null everywhere silently does nothing.
            "sector": [r[1] or r[2] or "UNCLASSIFIED" for r in rows],
            "shares_outstanding": [float(r[3]) if r[3] is not None else None for r in rows],
        },
        schema={
            "symbol": pl.Utf8,
            "sector": pl.Utf8,
            "shares_outstanding": pl.Float64,
        },
    )


async def load_panel(
    session: AsyncSession,
    *,
    as_of: date,
    lookback_sessions: int,
    market: str = "TWSE",
    symbols: list[str] | None = None,
    prefer_adjusted: bool = True,
    index_code: str = BENCHMARK_INDEX,
) -> tuple[panel_mod.Panel, str | None]:
    """Build a panel ending at `as_of`, and say why it is raw if it is.

    Returns the panel and, when the adjusted series was refused, the reason —
    which is carried into the run report rather than logged and forgotten.
    """
    start = as_of - timedelta(days=int(lookback_sessions * CALENDAR_SLACK) + 10)

    basis = panel_mod.PriceBasis.RAW
    blocked: str | None = None
    if prefer_adjusted:
        universe = symbols or await _symbols_with_prices(session, market, start, as_of)
        try:
            await CorporateActionCoverageService(session).assert_adjustable(
                universe, start, as_of, market
            )
            basis = panel_mod.PriceBasis.ADJUSTED
        except AdjustmentNotPermitted as exc:
            # Not fatal. The compromise is explicit and travels with every row
            # this panel produces; the alternative is computing nothing at all
            # until a source that does not exist yet is ingested.
            blocked = str(exc).split("\n")[0]

    price_column = (
        DailyPrice.adjusted_close if basis is panel_mod.PriceBasis.ADJUSTED else DailyPrice.close
    )
    query = select(
        DailyPrice.symbol,
        DailyPrice.trading_date,
        DailyPrice.open,
        DailyPrice.high,
        DailyPrice.low,
        price_column.label("close"),
        DailyPrice.volume,
        DailyPrice.turnover,
        DailyPrice.is_suspended,
    ).where(
        DailyPrice.market == market,
        DailyPrice.trading_date >= start,
        DailyPrice.trading_date <= as_of,
    )
    if symbols:
        query = query.where(DailyPrice.symbol.in_(symbols))
    rows = (await session.execute(query.order_by(DailyPrice.symbol, DailyPrice.trading_date))).all()

    def _f(value: object) -> float | None:
        return float(value) if isinstance(value, Decimal | int | float) else None

    frame = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "trading_date": [r[1] for r in rows],
            "open": [_f(r[2]) for r in rows],
            "high": [_f(r[3]) for r in rows],
            "low": [_f(r[4]) for r in rows],
            "close": [_f(r[5]) for r in rows],
            "volume": [_f(r[6]) for r in rows],
            "turnover": [_f(r[7]) for r in rows],
            "is_suspended": [bool(r[8]) for r in rows],
        },
        schema={
            "symbol": pl.Utf8,
            "trading_date": pl.Date,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
            "turnover": pl.Float64,
            "is_suspended": pl.Boolean,
        },
    )

    if not frame.is_empty():
        benchmark = await _benchmark_frame(session, start, as_of, index_code)
        if not benchmark.is_empty():
            frame = frame.join(benchmark, on="trading_date", how="left")
        master = await _master_frame(session, market)
        if not master.is_empty():
            frame = frame.join(master, on="symbol", how="left")

    return (
        panel_mod.build(frame, price_basis=basis, benchmark=index_code),
        blocked,
    )


async def _symbols_with_prices(
    session: AsyncSession, market: str, start: date, end: date
) -> list[str]:
    rows = (
        await session.execute(
            select(DailyPrice.symbol)
            .where(
                DailyPrice.market == market,
                DailyPrice.trading_date >= start,
                DailyPrice.trading_date <= end,
            )
            .distinct()
        )
    ).all()
    return sorted(r[0] for r in rows)


# ---------------------------------------------------------------- computing
async def compute_for_date(
    session: AsyncSession,
    *,
    trading_date: date,
    market: str = "TWSE",
    symbols: list[str] | None = None,
    prefer_adjusted: bool = True,
    persist: bool = True,
) -> QuantRunReport:
    """Indicators and factors for one session, across the whole market.

    One function rather than two because the factors read the indicators: doing
    them separately would either recompute the indicators or read them back out
    of the database, and reading them back means a factor's inputs depend on
    what a previous job happened to write.
    """
    started = time.perf_counter()
    version = await register_feature_version(session)

    lookback = max(ind.MAX_LOOKBACK, fct.REGISTRY.max_lookback())
    load_started = time.perf_counter()
    price_panel, blocked = await load_panel(
        session,
        as_of=trading_date,
        lookback_sessions=lookback,
        market=market,
        symbols=symbols,
        prefer_adjusted=prefer_adjusted,
    )
    report = QuantRunReport(
        trading_date=trading_date,
        feature_version=version,
        price_basis=str(price_panel.price_basis),
        adjustment_blocked=blocked,
    )
    report.timings_ms["load"] = int((time.perf_counter() - load_started) * 1000)

    if price_panel.frame.is_empty():
        report.timings_ms["total"] = int((time.perf_counter() - started) * 1000)
        log.warning("quant_no_prices", trading_date=str(trading_date), market=market)
        return report

    report.symbols = len(price_panel.symbols)

    indicator_started = time.perf_counter()
    indicator_frame = ind.compute(price_panel)
    report.timings_ms["indicators"] = int((time.perf_counter() - indicator_started) * 1000)
    report.indicators_unavailable = {
        k: list(v) for k, v in ind.unavailable_specs(price_panel.frame.columns).items()
    }
    report.indicators_computed = len(ind.REGISTRY) - len(report.indicators_unavailable)

    joined = price_panel.frame.join(indicator_frame, on=["symbol", "trading_date"], how="left")

    factor_started = time.perf_counter()
    computable = fct.REGISTRY.computable(joined.columns)
    report.factors_not_computable = {
        k: list(v) for k, v in fct.REGISTRY.not_computable(joined.columns).items()
    }
    report.factors_computed = len(computable)

    raw = fct.compute_raw(joined, computable)
    # Raw factors need the whole lookback (a 60-day momentum reads 60 sessions),
    # but the cross-sectional pipeline is a function of one date's values only
    # (see factors/pipeline.py), and only `trading_date` is written. Processing
    # every session in the lookback produced identical output for this date,
    # once per session instead of once: on Docker Desktop (16 polars threads)
    # a single call took minutes and the integration suite never finished.
    raw_today = {name: f.filter(pl.col("trading_date") == trading_date) for name, f in raw.items()}
    processed = _process_factors(joined, computable, raw_today)
    report.timings_ms["factors"] = int((time.perf_counter() - factor_started) * 1000)

    if persist:
        persist_started = time.perf_counter()
        report.indicator_rows = await _write_indicators(
            session,
            indicator_frame.filter(pl.col("trading_date") == trading_date),
            market=market,
            version=version,
            price_basis=str(price_panel.price_basis),
        )
        report.factor_rows = await _write_factors(
            session,
            processed,
            market=market,
            version=version,
            only_date=trading_date,
        )
        report.timings_ms["persist"] = int((time.perf_counter() - persist_started) * 1000)

    report.timings_ms["total"] = int((time.perf_counter() - started) * 1000)
    log.info("quant_computed", **report.as_dict())
    return report


def _process_factors(
    joined: pl.DataFrame,
    specs: list[fct.FactorSpec],
    raw: dict[str, pl.DataFrame],
) -> dict[str, pl.DataFrame]:
    """Run each factor's raw values through the cross-sectional pipeline.

    Size neutralisation needs a size control, and the control is the raw
    `log_market_cap` factor rather than a second definition of market cap — one
    definition, used twice.
    """
    controls = joined.select("symbol", "trading_date")
    if "sector" in joined.columns:
        controls = joined.select("symbol", "trading_date", "sector")
    if "log_market_cap" in raw:
        size = raw["log_market_cap"].rename({"raw_value": "log_market_cap"})
        controls = controls.join(size, on=["symbol", "trading_date"], how="left")

    out: dict[str, pl.DataFrame] = {}
    for spec in specs:
        frame = raw[spec.name].join(controls, on=["symbol", "trading_date"], how="left")
        out[spec.name] = fct.process(frame, spec)
    return out


# -------------------------------------------------------------- persistence
async def _write_indicators(
    session: AsyncSession,
    frame: pl.DataFrame,
    *,
    market: str,
    version: str,
    price_basis: str,
) -> int:
    if frame.is_empty():
        return 0
    columns = [c for c in frame.columns if c not in ("symbol", "trading_date")]
    payload: list[dict[str, Any]] = []
    for row in frame.iter_rows(named=True):
        values = {c: row[c] for c in columns if row[c] is not None}
        if not values:
            # A symbol with no computable indicator at all — a listing three
            # days old — is not written. An empty JSONB object would claim the
            # indicators were computed and found to be nothing.
            continue
        payload.append(
            {
                "symbol": row["symbol"],
                "market": market,
                "trading_date": row["trading_date"],
                "indicators": values,
                "price_basis": price_basis,
                "feature_version": version,
                "computed_at": datetime.now(UTC),
            }
        )
    return await _upsert(
        session,
        TechnicalIndicator,
        payload,
        constraint="uq_technical_indicators_key",
        update=("indicators", "price_basis", "computed_at", "market"),
    )


async def _write_factors(
    session: AsyncSession,
    processed: dict[str, pl.DataFrame],
    *,
    market: str,
    version: str,
    only_date: date | None = None,
) -> int:
    payload: list[dict[str, Any]] = []
    for name, frame in processed.items():
        if frame.is_empty():
            continue
        spec = fct.REGISTRY[name]
        rows = frame.filter(pl.col("trading_date") == only_date) if only_date else frame
        for row in rows.iter_rows(named=True):
            if row["raw_value"] is None and row["score"] is None:
                continue
            payload.append(
                {
                    "symbol": row["symbol"],
                    "market": market,
                    "trading_date": row["trading_date"],
                    "factor_name": name,
                    "category": str(spec.category),
                    "raw_value": row["raw_value"],
                    "zscore": row["zscore"],
                    "percentile": row["percentile"],
                    "sector_zscore": row["sector_zscore"],
                    "score": row["score"],
                    "feature_version": version,
                    "computed_at": datetime.now(UTC),
                }
            )
    return await _upsert(
        session,
        FactorScore,
        payload,
        constraint="uq_factor_scores_key",
        update=(
            "category",
            "raw_value",
            "zscore",
            "percentile",
            "sector_zscore",
            "score",
            "computed_at",
            "market",
        ),
    )


async def _upsert(
    session: AsyncSession,
    model: type[Any],
    payload: list[dict[str, Any]],
    *,
    constraint: str,
    update: tuple[str, ...],
    chunk: int = 2000,
) -> int:
    """Chunked idempotent write.

    Chunked for the same reason the ingestion pipeline is: PostgreSQL's wire
    protocol allows 32767 bind parameters per statement, and a full market of
    factor scores is a few hundred thousand.
    """
    written = 0
    for offset in range(0, len(payload), chunk):
        batch = payload[offset : offset + chunk]
        if not batch:
            continue
        stmt = pg_insert(model).values(batch)
        stmt = stmt.on_conflict_do_update(
            constraint=constraint,
            set_={key: getattr(stmt.excluded, key) for key in update},
        )
        await session.execute(stmt)
        written += len(batch)
    await session.flush()
    return written


# ---------------------------------------------------------------- validation
async def evaluate_factors(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    market: str = "TWSE",
    horizon: int = 20,
    persist: bool = True,
) -> list[fct.FactorValidity]:
    """Measure every factor over a window and record the verdicts.

    Factors that cannot be computed at all get a `NOT_COMPUTABLE` record naming
    the missing input. That is the point: a report with twenty rows where the
    catalogue has fifty-three would say nothing about the thirty-three.
    """
    version = await register_feature_version(session)
    lookback = max(ind.MAX_LOOKBACK, fct.REGISTRY.max_lookback())
    price_panel, _blocked = await load_panel(
        session,
        as_of=end,
        # The window plus the deepest lookback: a factor evaluated from `start`
        # needs its own history before `start`, or the first months of the
        # sample would be empty and the IC would be measured on whatever
        # survived.
        lookback_sessions=lookback + (end - start).days,
        market=market,
    )
    if price_panel.frame.is_empty():
        return []

    indicator_frame = ind.compute(price_panel)
    joined = price_panel.frame.join(indicator_frame, on=["symbol", "trading_date"], how="left")
    computable = fct.REGISTRY.computable(joined.columns)
    raw = fct.compute_raw(joined, computable)
    processed = _process_factors(joined, computable, raw)

    window = {
        name: frame.filter(pl.col("trading_date") >= start) for name, frame in processed.items()
    }
    returns = fct.forward_returns(price_panel.frame, horizon)
    universe = len(price_panel.symbols)

    reports: dict[str, fct.FactorValidity] = {}
    for name, frame in window.items():
        reports[name] = fct.evaluate(frame, returns, factor=name, universe_size=universe)

    matrix = fct.correlation_matrix(window)
    fct.attach_correlations(reports, matrix)
    for name in list(reports):
        reports[name] = fct.apply_thresholds(reports[name])

    for name, missing in fct.REGISTRY.not_computable(joined.columns).items():
        reports[name] = fct.FactorValidity(
            factor=name,
            verdict=Verdict.NOT_COMPUTABLE,
            missing_inputs=list(missing),
        )

    if persist:
        payload = [
            {
                "factor_name": name,
                "feature_version": version,
                "evaluated_from": start,
                "evaluated_to": end,
                "horizon_days": horizon,
                "verdict": str(report.verdict),
                "metrics": report.as_dict(),
                "evaluated_at": datetime.now(UTC),
            }
            for name, report in reports.items()
        ]
        await _upsert(
            session,
            FactorValidityRecord,
            payload,
            constraint="uq_factor_validity_key",
            update=("verdict", "metrics", "horizon_days", "evaluated_at"),
        )

    return [reports[name] for name in sorted(reports)]


async def recompute_history(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    market: str = "TWSE",
    prefer_adjusted: bool = True,
) -> dict[str, Any]:
    """Backfill indicators and factors across a date range.

    Loads the panel once and walks the dates inside it, rather than reloading
    per day. Ten years of a full market is about 320 MB — the reason this
    project is a single process with polars rather than a cluster.
    """
    version = await register_feature_version(session)
    lookback = max(ind.MAX_LOOKBACK, fct.REGISTRY.max_lookback())
    price_panel, blocked = await load_panel(
        session,
        as_of=end,
        lookback_sessions=lookback + (end - start).days,
        market=market,
        prefer_adjusted=prefer_adjusted,
    )
    if price_panel.frame.is_empty():
        return {"dates": 0, "indicator_rows": 0, "factor_rows": 0, "blocked": blocked}

    indicator_frame = ind.compute(price_panel)
    joined = price_panel.frame.join(indicator_frame, on=["symbol", "trading_date"], how="left")
    computable = fct.REGISTRY.computable(joined.columns)
    raw = fct.compute_raw(joined, computable)
    processed = _process_factors(joined, computable, raw)

    in_window = indicator_frame.filter(
        (pl.col("trading_date") >= start) & (pl.col("trading_date") <= end)
    )
    indicator_rows = await _write_indicators(
        session,
        in_window,
        market=market,
        version=version,
        price_basis=str(price_panel.price_basis),
    )
    factor_rows = 0
    for name, frame in processed.items():
        factor_rows += await _write_factors(
            session,
            {
                name: frame.filter(
                    (pl.col("trading_date") >= start) & (pl.col("trading_date") <= end)
                )
            },
            market=market,
            version=version,
        )

    return {
        "dates": in_window["trading_date"].n_unique(),
        "symbols": len(price_panel.symbols),
        "indicator_rows": indicator_rows,
        "factor_rows": factor_rows,
        "feature_version": version,
        "price_basis": str(price_panel.price_basis),
        "blocked": blocked,
    }


__all__ = [
    "BENCHMARK_INDEX",
    "FEATURE_SET",
    "QuantRunReport",
    "compute_for_date",
    "evaluate_factors",
    "load_panel",
    "recompute_history",
    "register_feature_version",
    "spec_document",
    "version_string",
]
