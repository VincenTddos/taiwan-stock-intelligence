"""Quant CLI: compute indicators and factors, and grade them.

    python -m scripts.quant compute --date 2026-08-14
    python -m scripts.quant backfill --from 2019-01-01 --to 2026-08-14
    python -m scripts.quant validate --from 2024-01-01 --to 2026-08-14
    python -m scripts.quant catalogue

The same functions Celery would call, runnable directly — which is how the
performance budget in `QUANT_ENGINE.md` §9 gets measured on real data rather
than asserted.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, get_sessionmaker
from app.quant import factors as fct
from app.quant import indicators as ind
from app.quant import jobs

log = get_logger(__name__)


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def run_compute(trading_date: date, prefer_adjusted: bool) -> int:
    async with get_sessionmaker()() as session:
        report = await jobs.compute_for_date(
            session, trading_date=trading_date, prefer_adjusted=prefer_adjusted
        )
        await session.commit()

    print(f"QUANT {trading_date}  version {report.feature_version}")
    print(f"  prices      {report.symbols} symbols, basis {report.price_basis}")
    if report.adjustment_blocked:
        # Not a warning to be scrolled past. Every indicator in this run was
        # computed on unadjusted prices, and the reason belongs on screen.
        print(f"  ADJUSTMENT  refused: {report.adjustment_blocked}")
    print(f"  indicators  {report.indicators_computed} computed, {report.indicator_rows} rows")
    print(f"  factors     {report.factors_computed} computed, {report.factor_rows} rows")
    if report.factors_not_computable:
        waiting = sorted(
            {column for columns in report.factors_not_computable.values() for column in columns}
        )
        print(
            f"  waiting on  {len(report.factors_not_computable)} factors, "
            f"{len(waiting)} missing inputs: {', '.join(waiting[:8])}"
            + (" …" if len(waiting) > 8 else "")
        )
    timings = " ".join(f"{k}={v}ms" for k, v in report.timings_ms.items())
    print(f"  timings     {timings}")
    return 0


async def run_backfill(start: date, end: date, prefer_adjusted: bool) -> int:
    async with get_sessionmaker()() as session:
        result = await jobs.recompute_history(
            session, start=start, end=end, prefer_adjusted=prefer_adjusted
        )
        await session.commit()
    print(f"QUANT BACKFILL {start} to {end}")
    for key in ("dates", "symbols", "indicator_rows", "factor_rows", "price_basis"):
        if key in result:
            print(f"  {key:14} {result[key]}")
    if result.get("blocked"):
        print(f"  adjustment     refused: {result['blocked']}")
    return 0


async def run_validate(start: date, end: date, horizon: int) -> int:
    async with get_sessionmaker()() as session:
        reports = await jobs.evaluate_factors(session, start=start, end=end, horizon=horizon)
        await session.commit()

    if not reports:
        print("no factor scores could be evaluated over that window", file=sys.stderr)
        return 2

    order = {"PASS": 0, "RESEARCH_ONLY": 1, "INSUFFICIENT_DATA": 2, "NOT_COMPUTABLE": 3}
    reports.sort(key=lambda r: (order.get(str(r.verdict), 9), r.factor))

    print(f"FACTOR VALIDITY {start} to {end}  horizon {horizon} sessions")
    print(f"  {'factor':28} {'verdict':18} {'IC':>8} {'RankIC':>8} {'IC-IR':>8} {'dates':>6}")
    for report in reports:

        def _fmt(value: float | None) -> str:
            return f"{value:8.4f}" if value is not None else f"{'—':>8}"

        print(
            f"  {report.factor:28} {report.verdict!s:18} "
            f"{_fmt(report.ic)} {_fmt(report.rank_ic)} {_fmt(report.ic_ir)} "
            f"{report.dates:6d}"
        )
        if report.failures:
            print(f"      {'; '.join(report.failures[:3])}")
        if report.missing_inputs:
            print(f"      waiting on {', '.join(report.missing_inputs)}")

    passed = sum(1 for r in reports if str(r.verdict) == "PASS")
    print(f"\n  {passed} of {len(reports)} factors passed every threshold")
    return 0


async def run_catalogue() -> int:
    async with get_sessionmaker()() as session:
        version = await jobs.register_feature_version(session)
        await session.commit()
    print(f"feature version {version}")
    print(
        f"  indicators {len(ind.REGISTRY)} across "
        f"{len({s.category for s in ind.REGISTRY.values()})} categories"
    )
    print(f"  factors    {len(fct.REGISTRY)} declared")
    by_category: dict[str, int] = {}
    for spec in fct.REGISTRY:
        by_category[str(spec.category)] = by_category.get(str(spec.category), 0) + 1
    for category, count in sorted(by_category.items()):
        print(f"    {category:14} {count}")
    return 0


def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)

    parser = argparse.ArgumentParser(prog="quant", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_compute = sub.add_parser("compute", help="indicators and factors for one session")
    p_compute.add_argument("--date", type=_date, required=True, dest="trading_date")
    p_compute.add_argument(
        "--raw-prices",
        action="store_true",
        help="skip the adjusted-price guard and compute on raw prices deliberately",
    )

    p_backfill = sub.add_parser("backfill", help="recompute a date range")
    p_backfill.add_argument("--from", type=_date, required=True, dest="start")
    p_backfill.add_argument("--to", type=_date, required=True, dest="end")
    p_backfill.add_argument("--raw-prices", action="store_true")

    p_validate = sub.add_parser("validate", help="grade every factor over a window")
    p_validate.add_argument("--from", type=_date, required=True, dest="start")
    p_validate.add_argument("--to", type=_date, required=True, dest="end")
    p_validate.add_argument("--horizon", type=int, default=20)

    sub.add_parser("catalogue", help="print the registry and register the feature version")

    args = parser.parse_args()
    if args.command == "compute":
        coro = run_compute(args.trading_date, not args.raw_prices)
    elif args.command == "backfill":
        coro = run_backfill(args.start, args.end, not args.raw_prices)
    elif args.command == "validate":
        coro = run_validate(args.start, args.end, args.horizon)
    else:
        coro = run_catalogue()

    async def _run() -> int:
        try:
            return await coro
        finally:
            await dispose_engine()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
