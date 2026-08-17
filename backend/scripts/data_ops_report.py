"""Run the full data-operations measurement and print it in one block.

PHASE_2_REPORT §11 was a list of commands whose combined output had to be
assembled by hand. This is the same sequence as one program, so what comes
back is complete and comparable rather than whatever happened to scroll past:
provider record counts, what actually landed, coverage, and every failure and
gap, for each dataset.

    python -m scripts.data_ops_report --year 2026
    python -m scripts.data_ops_report --year 2026 --json report.json

Nothing here estimates. Every number is either read from a provider response or
counted in the database, and anything that could not be measured is printed as
the reason it could not be measured. A dataset that fails is reported as failed;
it is never omitted, and never replaced with a zero that reads like a
measurement.

The transport is stated at the top. If PROVIDER_MODE is not `live` the numbers
describe recorded fixtures, not the exchange, and the banner says so — the point
of this report is to be quotable, so it has to be unambiguous about what it
measured.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, get_sessionmaker
from app.ingest import jobs
from app.models.market import (
    CorporateAction,
    DailyPrice,
    IndexQuote,
    InstitutionalFlow,
    StockMaster,
    TradingCalendar,
)
from app.models.ops import (
    BackfillCheckpoint,
    DataQuarantine,
    DataSource,
    RawIngestion,
)
from app.providers.registry import ProviderRegistry
from app.services.calendar_service import TradingCalendarService
from app.services.freshness_service import DataFreshnessService

log = get_logger(__name__)

W = 78


def rule(char: str = "-") -> str:
    return char * W


def head(title: str) -> None:
    print(f"\n{rule('=')}\n{title}\n{rule('=')}")


@dataclass
class StepResult:
    """One measured ingestion. `error` and `records_*` are mutually exclusive."""

    name: str
    ok: bool = False
    provider_records: int | None = None
    written: int = 0
    quarantined: int = 0
    suspect: int = 0
    parse_errors: int = 0
    data_as_of: date | None = None
    elapsed_ms: int = 0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def row(self) -> str:
        if not self.ok:
            return f"  {self.name:<20} FAILED   {(self.error or '')[:44]}"
        prov = "?" if self.provider_records is None else str(self.provider_records)
        return (
            f"  {self.name:<20} ok  provider={prov:>6}  written={self.written:>6}  "
            f"quarantined={self.quarantined:>5}  suspect={self.suspect:>4}"
        )


async def _timed(coro: Any) -> tuple[Any, int, str | None]:
    """Run a step, returning (result, elapsed_ms, error). Never raises.

    A report that dies on the first failing dataset tells you about one problem.
    This one is meant to tell you about all of them in a single run, so every
    step is allowed to fail on its own.
    """
    start = datetime.now(UTC)
    try:
        result = await coro
        err = None
    except Exception as exc:  # reported, never swallowed — see the docstring
        result = None
        err = f"{type(exc).__name__}: {exc}"
        log.warning("data_ops_report.step_failed", error=err, tb=traceback.format_exc())
    elapsed = int((datetime.now(UTC) - start).total_seconds() * 1000)
    return result, elapsed, err


# ------------------------------------------------------------------ sources
async def probe_sources(session: AsyncSession, registry: ProviderRegistry) -> list[dict[str, Any]]:
    rows = (await session.execute(select(DataSource).order_by(DataSource.code))).scalars().all()
    out: list[dict[str, Any]] = []
    for src in rows:
        entry: dict[str, Any] = {
            "code": src.code,
            "status": src.status,
            "base_url": src.base_url,
            "consecutive_failures": src.consecutive_failures,
            "last_error": src.last_error,
            "reachable": None,
            "probe_error": None,
        }
        try:
            provider = await registry.get(session, src.code)
        except Exception as exc:
            entry["probe_error"] = f"no provider implementation: {exc}"
            out.append(entry)
            continue
        health, elapsed, err = await _timed(provider.health())
        entry["probe_ms"] = elapsed
        if err:
            entry["reachable"] = False
            entry["probe_error"] = err
        else:
            entry["reachable"] = bool(getattr(health, "reachable", True))
            entry["probe_detail"] = getattr(health, "detail", None)
            if not entry["reachable"]:
                entry["probe_error"] = getattr(health, "error", None)
        out.append(entry)
    return out


# --------------------------------------------------------------- ingestion
async def run_ingestion(
    session: AsyncSession, registry: ProviderRegistry, *, year: int
) -> list[StepResult]:
    steps: list[StepResult] = []

    # Calendar first, always. Without it every record is quarantined for an
    # unknown trading date, and the resulting report would describe a data
    # problem that is really an ordering problem.
    res, ms, err = await _timed(jobs.ingest_trading_calendar(session, registry, year=year))
    if err:
        steps.append(StepResult("trading_calendar", error=err, elapsed_ms=ms))
        await session.rollback()
    else:
        await session.commit()
        steps.append(
            StepResult(
                "trading_calendar",
                ok=True,
                provider_records=res.get("closures_published"),
                written=res.get("total", 0),
                elapsed_ms=ms,
                detail=res,
            )
        )

    res, ms, err = await _timed(jobs.ingest_stock_master(session, registry))
    if err:
        steps.append(StepResult("stock_master", error=err, elapsed_ms=ms))
        await session.rollback()
    else:
        await session.commit()
        steps.append(
            StepResult(
                "stock_master",
                ok=True,
                provider_records=res.get("records_in"),
                written=res.get("inserted", 0) + res.get("updated", 0),
                quarantined=res.get("quarantined", 0),
                elapsed_ms=ms,
                detail=res,
            )
        )

    async def _outcome(name: str, coro: Any) -> None:
        outcome, ms_, err_ = await _timed(coro)
        if err_ or outcome is None:
            steps.append(StepResult(name, error=err_ or "no outcome", elapsed_ms=ms_))
            await session.rollback()
            return
        await session.commit()
        steps.append(
            StepResult(
                name,
                ok=True,
                provider_records=outcome.records_in,
                written=outcome.records_written,
                quarantined=outcome.records_quarantined,
                suspect=outcome.records_suspect,
                parse_errors=outcome.parse_errors,
                data_as_of=outcome.data_as_of,
                elapsed_ms=ms_,
                detail={"timings_ms": outcome.timings_ms, "status": outcome.status},
            )
        )

    await _outcome("index_quotes", jobs.ingest_index_quotes(session, registry))
    await _outcome("daily_prices", jobs.ingest_daily_prices(session, registry))

    target = await TradingCalendarService(session).previous_trading_day(date.today())
    if target is None:
        steps.append(StepResult("institutional_flow", error="no prior trading day in the calendar"))
    else:
        await _outcome(
            "institutional_flow",
            jobs.ingest_institutional_flow(session, registry, trading_date=target),
        )

    return steps


# ---------------------------------------------------------------- coverage
async def measure_coverage(session: AsyncSession) -> dict[str, Any]:
    async def span(model: Any, col: Any) -> dict[str, Any]:
        row = (
            await session.execute(
                select(func.count(), func.min(col), func.max(col)).select_from(model)
            )
        ).one()
        return {"rows": row[0], "from": row[1], "to": row[2]}

    async def distinct(model: Any, col: Any) -> int:
        return int(
            (
                await session.execute(select(func.count(func.distinct(col))).select_from(model))
            ).scalar_one()
        )

    cov: dict[str, Any] = {
        "trading_calendar": await span(TradingCalendar, TradingCalendar.calendar_date),
        "daily_prices": await span(DailyPrice, DailyPrice.trading_date),
        "index_quotes": await span(IndexQuote, IndexQuote.trading_date),
        "institutional_flow": await span(InstitutionalFlow, InstitutionalFlow.trading_date),
        "corporate_actions": await span(CorporateAction, CorporateAction.ex_date),
        "raw_ingestions": await span(RawIngestion, RawIngestion.data_as_of),
    }
    cov["daily_prices"]["symbols"] = await distinct(DailyPrice, DailyPrice.symbol)
    cov["institutional_flow"]["symbols"] = await distinct(
        InstitutionalFlow, InstitutionalFlow.symbol
    )
    cov["index_quotes"]["indices"] = await distinct(IndexQuote, IndexQuote.index_code)

    trading_days = (
        await session.execute(
            select(func.count())
            .select_from(TradingCalendar)
            .where(TradingCalendar.is_trading_day.is_(True))
        )
    ).scalar_one()
    cov["trading_calendar"]["trading_days"] = trading_days

    cov["stock_master"] = {
        "rows": int(
            (await session.execute(select(func.count()).select_from(StockMaster))).scalar_one()
        ),
        "current": int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(StockMaster)
                    .where(StockMaster.is_current.is_(True))
                )
            ).scalar_one()
        ),
    }

    transports = (
        await session.execute(
            select(RawIngestion.transport, func.count()).group_by(RawIngestion.transport)
        )
    ).all()
    cov["raw_ingestions"]["by_transport"] = dict(transports)

    # Trading days in range that have no price row at all. This is the gap
    # measurement that matters for a backtest: a missing session is not a zero,
    # and it must be visible rather than interpolated over.
    span_row = cov["daily_prices"]
    if span_row["from"] and span_row["to"]:
        expected = await TradingCalendarService(session).trading_days(
            span_row["from"], span_row["to"]
        )
        present = {
            d
            for (d,) in (
                await session.execute(
                    select(DailyPrice.trading_date)
                    .where(DailyPrice.trading_date.between(span_row["from"], span_row["to"]))
                    .distinct()
                )
            ).all()
        }
        missing = sorted(set(expected) - present)
        cov["daily_prices"]["expected_trading_days"] = len(expected)
        cov["daily_prices"]["days_with_data"] = len(present)
        cov["daily_prices"]["missing_days"] = [d.isoformat() for d in missing]
    return cov


# ------------------------------------------------------------------- gaps
async def measure_gaps(session: AsyncSession, coverage: dict[str, Any]) -> dict[str, Any]:
    quarantine = (
        await session.execute(
            select(DataQuarantine.dataset, DataQuarantine.severity, DataQuarantine.rule_ids)
        )
    ).all()
    by_dataset: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    for dataset, severity, rule_ids in quarantine:
        by_dataset[f"{dataset}/{severity}"] += 1
        for rid in rule_ids or []:
            by_rule[rid] += 1

    checkpoints = (await session.execute(select(BackfillCheckpoint))).scalars().all()
    freshness = await DataFreshnessService(session).evaluate_all()

    # Corroborate the published calendar against observed volume, over exactly
    # the range for which prices exist. Asking outside that range would report
    # every unpopulated day as a discrepancy, which is a coverage fact, not a
    # calendar fact, and the two must not be conflated.
    span = coverage.get("daily_prices", {})
    calendar_check: dict[str, Any]
    if not span.get("from") or not span.get("to"):
        calendar_check = {"skipped": "no daily prices ingested, nothing to corroborate against"}
    else:
        try:
            calendar_check = dict(
                await TradingCalendarService(session).verify_against_observations(
                    span["from"], span["to"]
                )
            )
            calendar_check["range"] = f"{span['from']} → {span['to']}"
        except Exception as exc:
            calendar_check = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "quarantine_total": len(quarantine),
        "quarantine_by_dataset": dict(by_dataset),
        "quarantine_by_rule": dict(by_rule),
        "calendar_verification": calendar_check,
        "freshness": [
            {
                "dataset": f["dataset"],
                "status": f["status"],
                "last_data_date": str(f.get("last_data_date")),
                "lag_minutes": f.get("lag_minutes"),
            }
            for f in freshness
        ],
        "backfill_checkpoints": [
            {
                "job_key": c.job_key,
                "status": c.status,
                "cursor": str(c.cursor),
                "range": f"{c.range_start} → {c.range_end}",
                "units_done": c.units_done,
                "units_total": c.units_total,
                "records_written": c.records_written,
                "units_failed": c.units_failed,
                "units_skipped": c.units_skipped,
                "last_error": c.last_error,
            }
            for c in checkpoints
        ],
    }


# ----------------------------------------------------------------- printing
def print_report(payload: dict[str, Any]) -> None:
    s = payload["environment"]
    head("twquant — data operations report")
    print(f"  generated      {payload['generated_at']}")
    print(f"  app env        {s['app_env']}")
    print(f"  provider mode  {s['provider_mode']}")
    print(f"  database       {s['database']}")
    if s["provider_mode"] != "live":
        print()
        print("  " + rule("!"))
        print("  THESE ARE NOT LIVE MEASUREMENTS.")
        print(f"  PROVIDER_MODE={s['provider_mode']} — every count below describes recorded")
        print("  fixtures replayed through the production parsers, not the exchange.")
        print("  Re-run with PROVIDER_MODE=live on a machine that can reach TWSE.")
        print("  " + rule("!"))

    head("1. Sources")
    for src in payload["sources"]:
        mark = "ok " if src["reachable"] else "DOWN" if src["reachable"] is False else "  ? "
        print(f"  {mark} {src['code']:<10} {src['status']:<11} {src['base_url']}")
        if src.get("probe_error"):
            print(f"       {src['probe_error'][:70]}")

    head("2. Ingestion — provider records in, rows out")
    for step in payload["steps"]:
        print(StepResult(**{**step, "data_as_of": None}).row())
    failed = [s["name"] for s in payload["steps"] if not s["ok"]]
    print(
        f"\n  {len(payload['steps']) - len(failed)}/{len(payload['steps'])} datasets ingested"
        + (f"; FAILED: {', '.join(failed)}" if failed else "")
    )

    head("3. Coverage")
    cov = payload["coverage"]
    for name in (
        "trading_calendar",
        "stock_master",
        "daily_prices",
        "index_quotes",
        "institutional_flow",
        "corporate_actions",
        "raw_ingestions",
    ):
        c = cov.get(name, {})
        extra = ""
        if "symbols" in c:
            extra = f"  symbols={c['symbols']}"
        elif "indices" in c:
            extra = f"  indices={c['indices']}"
        elif "current" in c:
            extra = f"  current={c['current']}"
        elif "trading_days" in c:
            extra = f"  trading_days={c['trading_days']}"
        rng = f"{c.get('from')} → {c.get('to')}" if c.get("from") else "—"
        print(f"  {name:<20} rows={c.get('rows', 0):>8}   {rng:<26}{extra}")
    dp = cov.get("daily_prices", {})
    if "expected_trading_days" in dp:
        miss = dp["missing_days"]
        print(
            f"\n  price completeness   {dp['days_with_data']}/{dp['expected_trading_days']} "
            f"trading days in range have data"
        )
        if miss:
            shown = ", ".join(miss[:12]) + (f" … +{len(miss) - 12} more" if len(miss) > 12 else "")
            print(f"  missing sessions     {shown}")
    if cov.get("raw_ingestions", {}).get("by_transport"):
        print(f"  ingestion transports {cov['raw_ingestions']['by_transport']}")

    head("4. Failures and gaps")
    g = payload["gaps"]
    print(f"  quarantined records  {g['quarantine_total']}")
    for k, v in sorted(g["quarantine_by_dataset"].items()):
        print(f"    {k:<34} {v}")
    if g["quarantine_by_rule"]:
        print("  by rule")
        for k, v in sorted(g["quarantine_by_rule"].items(), key=lambda kv: -kv[1]):
            print(f"    {k:<34} {v}")

    print("\n  freshness")
    for f in g["freshness"]:
        print(f"    {f['dataset']:<22} {f['status']:<9} last_data={f['last_data_date']}")

    cal = g["calendar_verification"]
    print("\n  calendar vs observed volume")
    if "error" in cal or "skipped" in cal:
        print(f"    not evaluated: {cal.get('error') or cal['skipped']}")
    else:
        for k, v in cal.items():
            print(f"    {k:<22} {v if not isinstance(v, list) else v[:10]}")

    if g["backfill_checkpoints"]:
        print("\n  backfill checkpoints")
        for c in g["backfill_checkpoints"]:
            print(
                f"    {c['job_key'][:44]:<46} {c['status']:<10} "
                f"{c['units_done']}/{c['units_total']} units, {c['records_written']} records"
            )
            if c["last_error"]:
                print(f"      last error: {c['last_error'][:60]}")
    else:
        print("\n  backfill checkpoints  none — no historical backfill has been run")

    print(f"\n{rule('=')}")


# --------------------------------------------------------------------- main
async def preflight(session: AsyncSession) -> str | None:
    """Return a human instruction if the database is not ready, else None.

    This script is meant to be run by someone on a machine that can reach the
    exchange, quite possibly for the first time. Meeting them with a sixty-line
    asyncpg traceback because migrations have not been applied is a bad answer
    to a question the script can just ask.
    """
    try:
        await session.execute(select(func.count()).select_from(DataSource))
    except Exception:
        await session.rollback()
        return (
            "the schema is not present in this database.\n"
            "    Run:  make migrate    (then)  make seed-sources"
        )
    count = (await session.execute(select(func.count()).select_from(DataSource))).scalar_one()
    if not count:
        return (
            "the data source registry is empty, so there is nothing to ingest from.\n"
            "    Run:  make seed-sources"
        )
    return None


async def build(year: int) -> dict[str, Any]:
    settings = get_settings()
    registry = ProviderRegistry(settings)
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        problem = await preflight(session)
        if problem is not None:
            await registry.aclose()
            return {"preflight_error": problem}

        sources = await probe_sources(session, registry)
        steps = await run_ingestion(session, registry, year=year)
        await jobs.refresh_market_status(session)
        await session.commit()
        coverage = await measure_coverage(session)
        gaps = await measure_gaps(session, coverage)
        await session.commit()

    await registry.aclose()

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "app_env": str(settings.APP_ENV),
            "provider_mode": str(settings.PROVIDER_MODE),
            "database": f"{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}",
        },
        "sources": sources,
        "steps": [
            {
                "name": s.name,
                "ok": s.ok,
                "provider_records": s.provider_records,
                "written": s.written,
                "quarantined": s.quarantined,
                "suspect": s.suspect,
                "parse_errors": s.parse_errors,
                "elapsed_ms": s.elapsed_ms,
                "error": s.error,
                "detail": s.detail,
            }
            for s in steps
        ],
        "coverage": coverage,
        "gaps": gaps,
    }


def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)

    parser = argparse.ArgumentParser(prog="data_ops_report", description=__doc__)
    parser.add_argument("--year", type=int, default=date.today().year)
    parser.add_argument(
        "--json", dest="json_path", default=None, help="also write the full structured report here"
    )
    args = parser.parse_args()

    async def _run() -> int:
        try:
            payload = await build(args.year)
        finally:
            await dispose_engine()

        if "preflight_error" in payload:
            print(f"\ncannot produce a report: {payload['preflight_error']}\n", file=sys.stderr)
            return 2

        print_report(payload)
        if args.json_path:
            with open(args.json_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str, ensure_ascii=False)
            print(f"  full structured report written to {args.json_path}")

        # Non-zero when a dataset failed, so this is usable in automation and
        # cannot be mistaken for a clean run by a script that only checks the
        # exit code.
        return 1 if any(not s["ok"] for s in payload["steps"]) else 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
