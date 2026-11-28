"""Ingestion CLI.

The same jobs Celery runs, callable directly. Useful for the first population of
a fresh database, for a one-off backfill, and for verifying a live provider on a
machine that can actually reach the exchange.

    python -m scripts.ingest calendar --year 2026
    python -m scripts.ingest master
    python -m scripts.ingest daily
    python -m scripts.ingest backfill --dataset institutional_flow \\
        --from 2019-01-01 --to 2026-08-15
    python -m scripts.ingest corporate-actions --from 2019-01-01 --to 2026-12-31
    python -m scripts.ingest adjust --from 2019-01-01 --to 2026-08-19 --symbol 2330
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, get_sessionmaker
from app.ingest import jobs
from app.models.market import StockMaster
from app.providers.base import BaseMarketDataProvider, ProviderError
from app.providers.registry import CORPORATE_ACTION_PROVIDER_TYPES, ProviderRegistry
from app.services.adjustment_service import AdjustmentService
from app.services.backfill_service import BackfillProgress, BackfillService
from app.services.calendar_service import ScheduleYearMismatch
from app.services.corporate_action_service import AdjustmentNotPermitted

log = get_logger(__name__)


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def run_calendar(year: int, replay_dir: Path | None = None) -> int:
    settings = get_settings()
    registry = ProviderRegistry(settings)
    provider: BaseMarketDataProvider | None = None
    if replay_dir is not None:
        # The holiday endpoint only serves the schedule currently published, so
        # a past year can only be rebuilt from a verbatim recording. The raw
        # ingestion is stamped REPLAY, never LIVE.
        from app.providers.replay import ReplayProvider

        provider = ReplayProvider(fixture_dir=replay_dir, settings=settings)
        print(f"calendar {year}: rebuilding from recording in {replay_dir} (REPLAY)")
    async with get_sessionmaker()() as session:
        try:
            result = await jobs.ingest_trading_calendar(
                session, registry, year=year, provider=provider
            )
        except ScheduleYearMismatch as exc:
            await session.rollback()
            await registry.aclose()
            print(f"calendar {year}: NOT built: {exc}", file=sys.stderr)
            return 2
        await session.commit()
    await registry.aclose()
    print(
        f"calendar {year}: {result['total']} days "
        f"({result['trading']} trading, {result['weekend']} weekend, "
        f"{result['holiday']} holiday)"
    )
    if result["annotation_rows_ignored"]:
        print(
            f"  {result['annotation_rows_ignored']} schedule rows were annotations "
            f"(e.g. 農曆春節前最後交易日) and correctly left as trading days"
        )
    return 0


async def run_master() -> int:
    settings = get_settings()
    registry = ProviderRegistry(settings)
    async with get_sessionmaker()() as session:
        result = await jobs.ingest_stock_master(session, registry)
        await session.commit()
    await registry.aclose()
    print(
        f"stock master: {result['inserted']} new, {result['updated']} changed, "
        f"{result['unchanged']} unchanged, {result['quarantined']} quarantined"
    )
    return 0


async def run_daily(trading_date: date | None) -> int:
    settings = get_settings()
    registry = ProviderRegistry(settings)
    exit_code = 0

    async with get_sessionmaker()() as session:
        for label, coro in (
            (
                "index_quotes",
                jobs.ingest_index_quotes(session, registry, trading_date=trading_date),
            ),
            (
                "daily_prices",
                jobs.ingest_daily_prices(session, registry, trading_date=trading_date),
            ),
        ):
            try:
                outcome = await coro
                await session.commit()
                print(
                    f"{label:20s} as_of={outcome.data_as_of} in={outcome.records_in} "
                    f"written={outcome.records_written} "
                    f"quarantined={outcome.records_quarantined} "
                    f"suspect={outcome.records_suspect}"
                )
            except ProviderError as exc:
                await session.rollback()
                print(f"{label:20s} FAILED: {exc}", file=sys.stderr)
                exit_code = 1

        # Institutional flow needs an explicit date; use the session we just got.
        target = trading_date
        if target is None:
            from app.services.calendar_service import TradingCalendarService

            target = await TradingCalendarService(session).previous_trading_day(
                date.today() + __import__("datetime").timedelta(days=1)
            )
        if target is not None:
            try:
                outcome = await jobs.ingest_institutional_flow(
                    session, registry, trading_date=target
                )
                await session.commit()
                print(
                    f"{'institutional_flow':20s} as_of={outcome.data_as_of} "
                    f"in={outcome.records_in} written={outcome.records_written}"
                )
            except ProviderError as exc:
                await session.rollback()
                print(f"{'institutional_flow':20s} FAILED: {exc}", file=sys.stderr)
                exit_code = 1

        await jobs.refresh_market_status(session)
        freshness = await jobs.refresh_freshness(session)
        await session.commit()

    await registry.aclose()
    print("\nfreshness:")
    for f in freshness:
        print(f"  {f['dataset']:20s} {f['status']:9s} last_data={f['last_data_date']}")
    return exit_code


async def run_backfill(dataset: str, start: date, end: date, symbol: str | None) -> int:
    settings = get_settings()
    registry = ProviderRegistry(settings)

    async with get_sessionmaker()() as session:
        provider = await registry.get(session, "TWSE")
        svc = BackfillService(session)

        job_key = f"{dataset}:{symbol or 'ALL'}:{start}:{end}"
        checkpoint = await svc.get_or_create(
            job_key=job_key,
            dataset=dataset,
            source="TWSE",
            range_start=start,
            range_end=end,
            scope=symbol or "ALL",
        )
        await session.commit()

        if dataset == "institutional_flow":

            async def fetch(p: BaseMarketDataProvider, day: date):  # type: ignore[no-untyped-def]
                return await p.get_institutional_flow(trading_date=day)
        elif dataset == "daily_prices":

            async def fetch(p: BaseMarketDataProvider, day: date):  # type: ignore[no-untyped-def]
                return await p.get_daily_prices(trading_date=day, symbol=symbol)
        else:
            print(f"no backfill strategy for dataset '{dataset}'", file=sys.stderr)
            return 2

        def show(progress: BackfillProgress) -> None:
            print(
                f"\r  {progress.bar()} {progress.pct:5.1f}%  "
                f"{progress.units_done}/{progress.units_total} units, "
                f"{progress.records_written} records",
                end="",
                flush=True,
            )

        print(f"BACKFILL {job_key}\n  resuming from {checkpoint.cursor}")
        progress = await svc.run(checkpoint, provider, fetch=fetch, on_progress=show)
        print()
        print(progress.render())

    await registry.aclose()
    return 0 if progress.status in ("COMPLETED", "PAUSED") else 1


async def run_corporate_actions(start: date, end: date, symbol: str | None) -> int:
    """Every registered corporate action source, in one pass.

    All of them, not one: each sees a different slice — the forecast table
    decomposes an event but only ahead of its ex-date, the results table has
    history and the exchange's own reference prices, capital reductions are a
    third endpoint again — and `assert_adjustable` unions their coverage.
    Running one alone leaves gaps that block adjustment, which is the correct
    outcome and a confusing one to debug.
    """
    settings = get_settings()
    registry = ProviderRegistry(settings)
    failures = 0
    async with get_sessionmaker()() as session:
        sources = await registry.active_sources(session)
        codes = [s.code for s in sources if s.provider_type in CORPORATE_ACTION_PROVIDER_TYPES]
        if not codes:
            print(
                "no corporate action source is registered and active; "
                "run `python -m scripts.seed_sources` first",
                file=sys.stderr,
            )
            return 2

        for code in codes:
            try:
                result = await jobs.ingest_corporate_actions(
                    session, registry, start=start, end=end, source=code, symbol=symbol
                )
                await session.commit()
            except ProviderError as exc:
                await session.rollback()
                failures += 1
                print(f"{code}: {exc}", file=sys.stderr)
                continue

            outcome = result["outcome"]
            covered = result["covered"]
            print(code)
            print(
                f"  actions   {outcome['records_written']} written, "
                f"{outcome['records_quarantined']} quarantined "
                f"(of {outcome['records_in']} parsed)"
            )
            if covered:
                print(
                    f"  coverage  {covered[0]} to {covered[1]} over "
                    f"{result['coverage_symbols']} symbols "
                    f"({result['symbols_with_actions']} with actions)"
                )
            else:
                # Not a failure. A forward-looking source asked about the past
                # has nothing to say, and saying nothing is the honest answer.
                print(f"  coverage  none — this source cannot reach {start} to {end}")
            print(f"  sees      {', '.join(result['supported_actions'])}")

    await registry.aclose()
    return 1 if failures else 0


async def run_adjust(start: date, end: date, symbol: str | None, force: bool) -> int:
    """Compute adjustment factors and write the back-adjusted close."""
    async with get_sessionmaker()() as session:
        if symbol:
            symbols = [symbol]
        else:
            rows = await session.execute(
                select(StockMaster.symbol)
                .where(StockMaster.is_current.is_(True))
                .order_by(StockMaster.symbol)
            )
            symbols = [r[0] for r in rows.all()]
        if not symbols:
            print("no symbols in the security master; run `ingest master` first", file=sys.stderr)
            return 2

        svc = AdjustmentService(session)
        try:
            report = await svc.apply(symbols, start=start, end=end, require_coverage=not force)
        except AdjustmentNotPermitted as exc:
            # The guard doing its job. Printed in full because the message names
            # every blocked symbol and what is missing for each.
            print(str(exc), file=sys.stderr)
            return 3
        await session.commit()

    print(f"ADJUST {start} to {end}")
    print(f"  symbols   {report.adjusted}/{report.symbols} adjusted")
    print(f"  events    {report.events} ex-dates, {report.rows_written} bars rewritten")
    for day in report.unresolved[:10]:
        print(f"  unresolved {day.symbol} {day.ex_date}: {'; '.join(day.unresolved)}")
    # Not fatal, and not silent either. A factor that disagrees with the
    # exchange's own reference price is either a parse error or a real oddity,
    # and both are worth a look before the series is trusted.
    for day in report.mismatched[:10]:
        print(
            f"  disagrees  {day.symbol} {day.ex_date}: computed {day.factor:.6f}, "
            f"published {day.published:.6f} ({day.residual_bps:.1f} bps)"
        )
    return 0


def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)

    parser = argparse.ArgumentParser(prog="ingest", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_cal = sub.add_parser("calendar", help="build the trading calendar for a year")
    p_cal.add_argument("--year", type=int, default=date.today().year)
    p_cal.add_argument(
        "--replay-dir",
        type=Path,
        default=None,
        help="rebuild from a verbatim recorded holiday schedule in this directory "
        "(for years the exchange no longer publishes); stamped REPLAY",
    )

    sub.add_parser("master", help="refresh the security master (SCD2)")

    p_daily = sub.add_parser("daily", help="ingest the latest session")
    p_daily.add_argument("--date", type=_date, default=None, dest="trading_date")

    p_bf = sub.add_parser("backfill", help="resumable historical backfill")
    p_bf.add_argument("--dataset", required=True, choices=["daily_prices", "institutional_flow"])
    p_bf.add_argument("--from", required=True, type=_date, dest="start")
    p_bf.add_argument("--to", required=True, type=_date, dest="end")
    p_bf.add_argument("--symbol", default=None)

    p_ca = sub.add_parser("corporate-actions", help="ingest corporate actions, record coverage")
    p_ca.add_argument("--from", required=True, type=_date, dest="start")
    p_ca.add_argument("--to", required=True, type=_date, dest="end")
    p_ca.add_argument("--symbol", default=None)

    p_adj = sub.add_parser("adjust", help="compute adjustment factors and adjusted closes")
    p_adj.add_argument("--from", required=True, type=_date, dest="start")
    p_adj.add_argument("--to", required=True, type=_date, dest="end")
    p_adj.add_argument("--symbol", default=None)
    p_adj.add_argument(
        "--force",
        action="store_true",
        help="adjust without the coverage guard — for diagnosis only; the "
        "resulting series may silently omit real adjustments",
    )

    args = parser.parse_args()

    if args.command == "calendar":
        coro = run_calendar(args.year, args.replay_dir)
    elif args.command == "master":
        coro = run_master()
    elif args.command == "daily":
        coro = run_daily(args.trading_date)
    elif args.command == "corporate-actions":
        coro = run_corporate_actions(args.start, args.end, args.symbol)
    elif args.command == "adjust":
        coro = run_adjust(args.start, args.end, args.symbol, args.force)
    else:
        coro = run_backfill(args.dataset, args.start, args.end, args.symbol)

    async def _run() -> int:
        # Dispose inside the same event loop. A second `asyncio.run` for
        # teardown closes connections against a loop that no longer exists,
        # which produces a wall of spurious "Event loop is closed" tracebacks
        # after an otherwise successful run.
        try:
            return await coro
        finally:
            await dispose_engine()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
