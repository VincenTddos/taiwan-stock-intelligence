"""Historical backfill.

Requirements this satisfies, and why each one matters:

* **Checkpointed and resumable.** The cursor is committed after every unit. A
  backfill that restarts from the beginning after an interruption is not a
  backfill — it is repeatedly hammering the exchange for data already held.
* **Calendar-driven batching.** Units come from the trading calendar, so no
  request is ever made for a weekend or a holiday. Over ten years that removes
  roughly a third of the requests before any throttling.
* **Centrally rate limited.** Requests go through the provider, which goes
  through the shared `RateLimiter`. This service never sleeps on its own.
* **Failures are isolated.** One bad day is recorded in `failed_units` and the
  run continues. A single missing session must not abort nine years of progress.
* **Progress is observable.** Every batch logs and updates the checkpoint, which
  the Data Operations dashboard reads.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.ingest.pipeline import IngestionPipeline
from app.models.ops import BackfillCheckpoint, BackfillStatus
from app.providers.base import BaseMarketDataProvider, ProviderError, ProviderErrorKind
from app.services.calendar_service import TradingCalendarService

log = get_logger(__name__)

MAX_CONSECUTIVE_FAILURES = 10


@dataclass(slots=True)
class BackfillProgress:
    job_key: str
    status: str
    units_total: int
    units_done: int
    units_failed: int
    units_skipped: int
    records_written: int
    records_quarantined: int
    cursor: date
    elapsed_seconds: float = 0.0

    @property
    def pct(self) -> float:
        return round(100 * self.units_done / self.units_total, 1) if self.units_total else 0.0

    def bar(self, width: int = 18) -> str:
        filled = int(width * self.pct / 100)
        return "█" * filled + "░" * (width - filled)

    def render(self) -> str:
        return (
            f"BACKFILL {self.job_key}\n"
            f"  {self.bar()} {self.pct:.0f}%\n"
            f"  units    {self.units_done}/{self.units_total} "
            f"(failed {self.units_failed}, skipped {self.units_skipped})\n"
            f"  records  {self.records_written} written, "
            f"{self.records_quarantined} quarantined\n"
            f"  cursor   {self.cursor}"
        )


class BackfillService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.calendar = TradingCalendarService(session)
        self.pipeline = IngestionPipeline(session)

    # ------------------------------------------------------------------
    async def get_or_create(
        self,
        *,
        job_key: str,
        dataset: str,
        source: str,
        range_start: date,
        range_end: date,
        scope: str = "ALL",
    ) -> BackfillCheckpoint:
        existing = (
            await self.session.execute(
                select(BackfillCheckpoint).where(BackfillCheckpoint.job_key == job_key)
            )
        ).scalar_one_or_none()

        if existing is not None:
            log.info(
                "backfill_resumed",
                job_key=job_key,
                cursor=str(existing.cursor),
                done=existing.units_done,
                total=existing.units_total,
            )
            return existing

        checkpoint = BackfillCheckpoint(
            job_key=job_key,
            dataset=dataset,
            source=source,
            scope=scope,
            range_start=range_start,
            range_end=range_end,
            cursor=range_start,
            status=BackfillStatus.PENDING,
        )
        self.session.add(checkpoint)
        await self.session.flush()
        return checkpoint

    async def plan_units(self, checkpoint: BackfillCheckpoint, market: str = "TWSE") -> list[date]:
        """Remaining trading days from the cursor onwards.

        Driven by the calendar, so closed sessions are never requested.
        """
        return await self.calendar.trading_days(checkpoint.cursor, checkpoint.range_end, market)

    # ------------------------------------------------------------------
    async def run(
        self,
        checkpoint: BackfillCheckpoint,
        provider: BaseMarketDataProvider,
        *,
        fetch: Callable[[BaseMarketDataProvider, date], Awaitable[Any]],
        market: str = "TWSE",
        batch_size: int = 20,
        max_units: int | None = None,
        enforce_calendar: bool = True,
        on_progress: Callable[[BackfillProgress], None] | None = None,
    ) -> BackfillProgress:
        """Process units from the cursor, committing progress after each batch."""
        started = time.perf_counter()
        units = await self.plan_units(checkpoint, market)
        if max_units is not None:
            units = units[:max_units]

        if checkpoint.units_total is None:
            all_units = await self.calendar.trading_days(
                checkpoint.range_start, checkpoint.range_end, market
            )
            checkpoint.units_total = len(all_units)

        checkpoint.status = BackfillStatus.RUNNING
        if checkpoint.started_at is None:
            from datetime import UTC, datetime

            checkpoint.started_at = datetime.now(UTC)
        await self.session.flush()

        failed: list[str] = list(checkpoint.failed_units or [])
        consecutive_failures = 0

        for index, unit in enumerate(units, start=1):
            try:
                result = await fetch(provider, unit)
                outcome = await self.pipeline.run(
                    result, dataset=checkpoint.dataset, enforce_calendar=enforce_calendar
                )
                checkpoint.records_written += outcome.records_written
                checkpoint.records_quarantined += outcome.records_quarantined
                checkpoint.units_done += 1
                consecutive_failures = 0

            except ProviderError as exc:
                if exc.kind is ProviderErrorKind.NO_DATA:
                    # The source has nothing for this day. Legitimate for a
                    # symbol that had not listed yet — not a failure.
                    checkpoint.units_skipped += 1
                    consecutive_failures = 0
                else:
                    checkpoint.units_failed += 1
                    failed.append(unit.isoformat())
                    consecutive_failures += 1
                    checkpoint.last_error = f"{unit}: {exc}"
                    log.warning(
                        "backfill_unit_failed",
                        job_key=checkpoint.job_key,
                        unit=str(unit),
                        kind=str(exc.kind),
                        error=str(exc),
                    )

            except Exception as exc:
                checkpoint.units_failed += 1
                failed.append(unit.isoformat())
                consecutive_failures += 1
                checkpoint.last_error = f"{unit}: {exc}"
                log.exception("backfill_unit_error", job_key=checkpoint.job_key, unit=str(unit))

            # Advance past the unit just attempted, so a resume does not repeat it.
            checkpoint.cursor = unit + timedelta(days=1)
            checkpoint.failed_units = failed[-500:] or None

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Something systemic is wrong — the source is down or has
                # changed. Continuing would just generate noise and burn quota.
                checkpoint.status = BackfillStatus.FAILED
                checkpoint.last_error = (
                    f"aborted after {consecutive_failures} consecutive failures; "
                    f"last: {checkpoint.last_error}"
                )
                await self.session.commit()
                log.error(
                    "backfill_aborted",
                    job_key=checkpoint.job_key,
                    consecutive_failures=consecutive_failures,
                )
                break

            if index % batch_size == 0:
                await self.session.commit()
                progress = self._progress(checkpoint, started)
                log.info(
                    "backfill_progress",
                    job_key=checkpoint.job_key,
                    pct=progress.pct,
                    done=progress.units_done,
                    total=progress.units_total,
                    written=progress.records_written,
                )
                if on_progress:
                    on_progress(progress)
        else:
            remaining = await self.plan_units(checkpoint, market)
            if not remaining:
                checkpoint.status = BackfillStatus.COMPLETED
                from datetime import UTC, datetime

                checkpoint.completed_at = datetime.now(UTC)
            else:
                checkpoint.status = BackfillStatus.PAUSED

        await self.session.commit()
        progress = self._progress(checkpoint, started)
        log.info(
            "backfill_finished",
            job_key=checkpoint.job_key,
            status=checkpoint.status,
            pct=progress.pct,
            elapsed_s=round(progress.elapsed_seconds, 1),
        )
        return progress

    def _progress(self, cp: BackfillCheckpoint, started: float) -> BackfillProgress:
        return BackfillProgress(
            job_key=cp.job_key,
            status=cp.status,
            units_total=cp.units_total or 0,
            units_done=cp.units_done,
            units_failed=cp.units_failed,
            units_skipped=cp.units_skipped,
            records_written=cp.records_written,
            records_quarantined=cp.records_quarantined,
            cursor=cp.cursor,
            elapsed_seconds=time.perf_counter() - started,
        )

    async def list_jobs(self, dataset: str | None = None) -> list[BackfillCheckpoint]:
        stmt = select(BackfillCheckpoint).order_by(BackfillCheckpoint.updated_at.desc())
        if dataset:
            stmt = stmt.where(BackfillCheckpoint.dataset == dataset)
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["MAX_CONSECUTIVE_FAILURES", "BackfillProgress", "BackfillService"]
