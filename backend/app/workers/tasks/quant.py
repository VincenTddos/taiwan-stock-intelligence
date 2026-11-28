"""The daily quant task.

Runs after the market data for a session has landed, and computes that
session's indicators and factor scores for the whole market.

Two things about how it picks its date.

It takes the **latest session that actually has prices**, not today, and not the
calendar's idea of the last trading day. If ingestion has not run, or ran and
failed, the right behaviour is to recompute the last session that has data
rather than to produce a day of nulls that looks like a computed day.

It is **idempotent by construction** — the writes upsert on
`(symbol, trading_date, feature_version)` — so a retry, a double schedule or a
manual rerun converge on the same rows. That is what makes it safe to schedule
rather than to trigger by hand.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from sqlalchemy import func, select

from app.core.logging import get_logger
from app.db.session import dispose_engine, get_sessionmaker
from app.models.market import DailyPrice
from app.quant import jobs
from app.workers.celery_app import celery_app

log = get_logger(__name__)


async def _latest_session(market: str) -> date | None:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(func.max(DailyPrice.trading_date)).where(DailyPrice.market == market)
            )
        ).scalar_one_or_none()


async def _run(trading_date: date | None, market: str) -> dict[str, Any]:
    day = trading_date or await _latest_session(market)
    if day is None:
        return {"skipped": "no prices have been ingested for this market", "market": market}

    async with get_sessionmaker()() as session:
        report = await jobs.compute_for_date(session, trading_date=day, market=market)
        await session.commit()
    return report.as_dict()


@celery_app.task(name="quant.daily", bind=True, max_retries=2, soft_time_limit=900)
def quant_daily_task(
    self: Any, trading_date: str | None = None, market: str = "TWSE"
) -> dict[str, Any]:
    """Indicators and factors for one session.

    The event loop is created and disposed inside the task. A Celery worker is a
    synchronous process, and holding an engine across tasks would bind its pool
    to a loop that no longer exists — the same defect the ingestion CLI
    documented, in a place where it would be harder to see.
    """
    day = date.fromisoformat(trading_date) if trading_date else None

    async def _wrapped() -> dict[str, Any]:
        try:
            return await _run(day, market)
        finally:
            await dispose_engine()

    try:
        result = asyncio.run(_wrapped())
    except Exception as exc:
        log.error("quant_daily_failed", error=str(exc), market=market, exc_info=True)
        raise self.retry(exc=exc, countdown=300) from exc

    log.info("quant_daily_complete", **{k: v for k, v in result.items() if k != "timings_ms"})
    return result


__all__ = ["quant_daily_task"]
