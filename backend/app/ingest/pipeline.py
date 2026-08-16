"""The ingestion pipeline.

    Provider → Raw Ingestion → Validation → Quarantine → Normalisation
             → Canonical Model → PostgreSQL

Every dataset flows through `IngestionPipeline.run`, so the guarantees below hold
uniformly rather than per-job:

* **Provenance first.** The `raw_ingestions` row is written before any canonical
  row, and every canonical row references it. There is no path that stores a
  price without recording where it came from.
* **Idempotent.** Writes are `ON CONFLICT DO UPDATE` on the natural key
  `(symbol, trading_date, source)`. Re-running yesterday's job updates in place;
  it never duplicates.
* **Nothing is dropped silently.** Records that fail validation, and rows the
  provider could not parse at all, both land in `data_quarantine` with the raw
  payload attached.
* **The calendar decides what a trading day is.** The date comes from the
  payload and is then checked against the calendar. A job's own execution date
  never becomes a trading date.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.market import (
    CorporateAction,
    DailyPrice,
    IndexQuote,
    InstitutionalFlow,
    TradingCalendar,
)
from app.models.ops import DataQuarantine, IngestionMetric, RawIngestion
from app.providers.base import ProviderResult
from app.services.calendar_service import TradingCalendarService
from app.services.quality_service import DataQualityService, ValidationResult

log = get_logger(__name__)


@dataclass(slots=True)
class IngestionOutcome:
    dataset: str
    source: str
    ingestion_id: int | None = None
    data_as_of: date | None = None
    records_in: int = 0
    records_written: int = 0
    records_quarantined: int = 0
    records_suspect: int = 0
    parse_errors: int = 0
    skipped_reason: str | None = None
    timings_ms: dict[str, int] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.skipped_reason:
            return "SKIPPED"
        if self.records_quarantined and not self.records_written:
            return "FAILED"
        return "PARTIAL" if self.records_quarantined else "SUCCESS"

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "source": self.source,
            "status": self.status,
            "ingestion_id": self.ingestion_id,
            "data_as_of": self.data_as_of.isoformat() if self.data_as_of else None,
            "records_in": self.records_in,
            "records_written": self.records_written,
            "records_quarantined": self.records_quarantined,
            "records_suspect": self.records_suspect,
            "parse_errors": self.parse_errors,
            "skipped_reason": self.skipped_reason,
            "timings_ms": self.timings_ms,
        }


# dataset -> (model, natural-key columns, columns updated on conflict)
TARGETS: dict[str, tuple[type[Any], list[str], list[str]]] = {
    "daily_prices": (
        DailyPrice,
        ["symbol", "trading_date", "source"],
        [
            "open",
            "high",
            "low",
            "close",
            "change",
            "volume",
            "turnover",
            "trade_count",
            "note",
            "is_suspended",
            "quality_status",
            "quality_flags",
            "ingestion_id",
            "market",
        ],
    ),
    "index_quotes": (
        IndexQuote,
        ["index_code", "trading_date", "source"],
        [
            "index_name",
            "market",
            "index_type",
            "open",
            "high",
            "low",
            "close",
            "change",
            "change_pct",
            "volume",
            "turnover",
            "quality_status",
            "ingestion_id",
        ],
    ),
    "institutional_flow": (
        InstitutionalFlow,
        ["symbol", "trading_date", "investor_type", "source"],
        [
            "market",
            "buy_volume",
            "sell_volume",
            "net_volume",
            "buy_value",
            "sell_value",
            "net_value",
            "quality_status",
            "ingestion_id",
        ],
    ),
    "corporate_actions": (
        CorporateAction,
        ["symbol", "action_type", "ex_date", "source"],
        [
            "market",
            "announced_at",
            "announced_at_is_estimated",
            "payment_date",
            "record_date",
            "cash_dividend",
            "stock_dividend",
            "split_ratio",
            "subscription_price",
            "subscription_ratio",
            "factor",
            "ingestion_id",
        ],
    ),
    "trading_calendar": (
        TradingCalendar,
        ["market", "calendar_date"],
        ["is_trading_day", "session_type", "holiday_name", "description", "source"],
    ),
}

VALIDATORS: dict[str, str] = {
    "daily_prices": "validate_daily_price",
    "index_quotes": "validate_index_quote",
    "institutional_flow": "validate_institutional_flow",
    "stock_master": "validate_stock_master",
}


class IngestionPipeline:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.quality = DataQualityService()
        self.calendar = TradingCalendarService(session)

    # ------------------------------------------------------------------
    async def record_ingestion(
        self, result: ProviderResult, *, job_run_id: int | None = None
    ) -> RawIngestion:
        """Write provenance before anything else touches the canonical tables."""
        meta = result.metadata
        row = RawIngestion(
            source=meta.source,
            source_endpoint=meta.source_endpoint,
            dataset=meta.dataset,
            params=meta.params or None,
            transport=str(meta.transport),
            source_request_at=meta.source_request_at,
            source_response_at=meta.source_response_at,
            data_as_of=meta.data_as_of,
            http_status=meta.http_status,
            duration_ms=meta.duration_ms,
            response_bytes=meta.response_bytes,
            response_hash=meta.response_hash,
            record_count=result.record_count,
            status="SUCCESS",
            job_run_id=job_run_id,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def quarantine(
        self,
        *,
        dataset: str,
        source: str,
        raw_record: dict[str, Any],
        rule_ids: list[str],
        errors: list[dict[str, Any]],
        severity: str = "FATAL",
        symbol: str | None = None,
        trading_date: date | None = None,
        ingestion_id: int | None = None,
        normalized: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            DataQuarantine(
                dataset=dataset,
                source=source,
                symbol=symbol,
                trading_date=trading_date,
                raw_record=_jsonable(raw_record),
                normalized_record=_jsonable(normalized) if normalized else None,
                rule_ids=rule_ids,
                errors=errors,
                severity=severity,
                ingestion_id=ingestion_id,
            )
        )

    # ------------------------------------------------------------------
    async def run(
        self,
        result: ProviderResult,
        *,
        dataset: str,
        enforce_calendar: bool = True,
        prev_closes: dict[str, Any] | None = None,
        job_run_id: int | None = None,
    ) -> IngestionOutcome:
        """Validate, quarantine and persist one provider result."""
        started = time.perf_counter()
        outcome = IngestionOutcome(
            dataset=dataset,
            source=result.metadata.source,
            data_as_of=result.metadata.data_as_of,
            records_in=result.record_count,
            parse_errors=len(result.parse_errors),
        )

        ingestion = await self.record_ingestion(result, job_run_id=job_run_id)
        outcome.ingestion_id = ingestion.id

        # Rows the provider could not parse are evidence of a schema change.
        for err in result.parse_errors:
            await self.quarantine(
                dataset=dataset,
                source=result.metadata.source,
                raw_record=err.get("raw", {}) if isinstance(err, dict) else {"raw": str(err)},
                rule_ids=["PARSE"],
                errors=[err] if isinstance(err, dict) else [{"reason": str(err)}],
                ingestion_id=ingestion.id,
            )
        outcome.records_quarantined += len(result.parse_errors)

        if not result.records:
            outcome.skipped_reason = "provider returned no records"
            await self._finish(outcome, ingestion, started)
            return outcome

        # --- the calendar guard ---------------------------------------
        validate_ms = time.perf_counter()
        rows: list[dict[str, Any]] = []
        validator: Callable[..., ValidationResult] | None = None
        if name := VALIDATORS.get(dataset):
            validator = getattr(self.quality, name)

        for rec in result.records:
            trading_date = rec.get("trading_date")

            if enforce_calendar and isinstance(trading_date, date):
                try:
                    cal = await self.calendar.get(trading_date, rec.get("market", "TWSE"))
                except Exception:
                    cal = None
                if cal is None:
                    await self.quarantine(
                        dataset=dataset,
                        source=result.metadata.source,
                        raw_record=rec,
                        rule_ids=["CAL00"],
                        errors=[
                            {
                                "rule_id": "CAL00",
                                "message": f"no calendar entry for {trading_date}; "
                                f"run the calendar sync before ingesting",
                            }
                        ],
                        symbol=rec.get("symbol"),
                        trading_date=trading_date,
                        ingestion_id=ingestion.id,
                    )
                    outcome.records_quarantined += 1
                    continue
                if not cal.is_trading_day:
                    # This is the holiday guard. A snapshot endpoint repeating the
                    # previous session must never be written under a closed date.
                    await self.quarantine(
                        dataset=dataset,
                        source=result.metadata.source,
                        raw_record=rec,
                        rule_ids=["CAL01"],
                        errors=[
                            {
                                "rule_id": "CAL01",
                                "message": f"{trading_date} is not a trading day "
                                f"({cal.session_type}: {cal.holiday_name})",
                            }
                        ],
                        symbol=rec.get("symbol"),
                        trading_date=trading_date,
                        ingestion_id=ingestion.id,
                    )
                    outcome.records_quarantined += 1
                    continue

            if validator is not None:
                kwargs: dict[str, Any] = {}
                if dataset == "daily_prices" and prev_closes:
                    kwargs["prev_close"] = prev_closes.get(rec.get("symbol", ""))
                vr = validator(rec, **kwargs)
                if not vr.accepted:
                    await self.quarantine(
                        dataset=dataset,
                        source=result.metadata.source,
                        raw_record=rec,
                        rule_ids=[v.rule_id for v in vr.fatal],
                        errors=[v.as_dict() for v in vr.violations],
                        symbol=rec.get("symbol"),
                        trading_date=trading_date if isinstance(trading_date, date) else None,
                        ingestion_id=ingestion.id,
                    )
                    outcome.records_quarantined += 1
                    continue
                if vr.warnings:
                    outcome.records_suspect += 1
                rec = {**rec, "quality_status": vr.quality_status}
                if vr.flags and dataset == "daily_prices":
                    rec["quality_flags"] = vr.flags

            rows.append(rec)

        outcome.timings_ms["validation"] = int((time.perf_counter() - validate_ms) * 1000)

        # --- persist ---------------------------------------------------
        persist_ms = time.perf_counter()
        if rows:
            outcome.records_written = await self.upsert(dataset, rows, ingestion_id=ingestion.id)
        outcome.timings_ms["persist"] = int((time.perf_counter() - persist_ms) * 1000)

        await self._finish(outcome, ingestion, started)
        return outcome

    # ------------------------------------------------------------------
    async def upsert(
        self, dataset: str, rows: list[dict[str, Any]], *, ingestion_id: int | None = None
    ) -> int:
        """Idempotent bulk write on the dataset's natural key."""
        if not rows:
            return 0
        try:
            model, key_cols, update_cols = TARGETS[dataset]
        except KeyError as exc:
            raise ValueError(f"no persistence target for dataset '{dataset}'") from exc

        columns = {c.name for c in model.__table__.columns}
        payload: list[dict[str, Any]] = []
        for rec in rows:
            row = {k: v for k, v in rec.items() if k in columns}
            if ingestion_id is not None and "ingestion_id" in columns:
                row["ingestion_id"] = ingestion_id
            payload.append(row)

        # Deduplicate within the batch: ON CONFLICT cannot resolve two rows with
        # the same key in a single statement ("cannot affect row a second time").
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in payload:
            deduped[tuple(row.get(k) for k in key_cols)] = row

        # Give every row an identical key set. A multi-row INSERT compiles from
        # the union of keys, and a row missing one of them makes SQLAlchemy try
        # to apply a column default inside a bound VALUES clause, which it
        # refuses to compile. Real case: institutional flow, where DEALER and
        # TOTAL rows carry only a net volume while the others carry buy and sell.
        present = {k for row in deduped.values() for k in row}
        values = [{k: row.get(k) for k in present} for row in deduped.values()]

        stmt = pg_insert(model).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=key_cols,
            set_={c: getattr(stmt.excluded, c) for c in update_cols if c in present},
        )
        await self.session.execute(stmt)
        return len(deduped)

    async def _finish(
        self, outcome: IngestionOutcome, ingestion: RawIngestion, started: float
    ) -> None:
        total_ms = int((time.perf_counter() - started) * 1000)
        outcome.timings_ms["total"] = total_ms

        ingestion.accepted_count = outcome.records_written
        ingestion.quarantined_count = outcome.records_quarantined
        ingestion.status = outcome.status if outcome.status != "SKIPPED" else "SUCCESS"

        self.session.add(
            IngestionMetric(
                dataset=outcome.dataset,
                source=outcome.source,
                trading_date=outcome.data_as_of,
                provider_ms=ingestion.duration_ms,
                validation_ms=outcome.timings_ms.get("validation"),
                persist_ms=outcome.timings_ms.get("persist"),
                total_ms=total_ms,
                records_in=outcome.records_in,
                records_written=outcome.records_written,
                records_quarantined=outcome.records_quarantined,
                rows_per_second=(
                    round(outcome.records_written / (total_ms / 1000), 2)
                    if total_ms > 0 and outcome.records_written
                    else None
                ),
                ingestion_id=ingestion.id,
            )
        )
        await self.session.flush()

        log.info(
            "ingestion_complete",
            dataset=outcome.dataset,
            source=outcome.source,
            status=outcome.status,
            data_as_of=str(outcome.data_as_of),
            records_in=outcome.records_in,
            written=outcome.records_written,
            quarantined=outcome.records_quarantined,
            suspect=outcome.records_suspect,
            total_ms=total_ms,
        )


def _jsonable(value: Any) -> Any:
    """Coerce Decimals, dates and the like so JSONB serialisation cannot fail.

    A quarantine write that itself fails would lose the very record we are trying
    not to lose.
    """
    import datetime as dt
    import decimal

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


__all__ = ["TARGETS", "IngestionOutcome", "IngestionPipeline"]
