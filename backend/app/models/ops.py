"""Data-operations model: registry, provenance, quarantine, freshness, backfill.

These tables are what make the market data *trustworthy* rather than merely
present. Without them the platform can show a number; with them it can answer
where the number came from, when it arrived, what it represents, whether it is
still current, and what was rejected on the way in.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SourceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"
    UNVERIFIED = "UNVERIFIED"


class Transport(StrEnum):
    """How the bytes were obtained.

    LIVE  — an HTTP request to the source, right then.
    REPLAY— a previously recorded response from the same source, replayed.
            The *data* is still genuine exchange data; only the transport is
            recorded. Kept distinct so provenance never implies a fetch that
            did not happen.
    MOCK  — fabricated. Forbidden outside local/test.
    """

    LIVE = "LIVE"
    REPLAY = "REPLAY"
    MOCK = "MOCK"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"
    DEGRADED = "DEGRADED"


class BackfillStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# --------------------------------------------------------------------------
class DataSource(Base):
    """Registry of every external source.

    Base URLs, rate limits and coverage live here, not scattered through service
    code. Changing a rate limit is a row update, not a deploy.
    """

    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    provider_type: Mapped[str] = mapped_column(String(30), nullable=False)  # TWSE/TPEX/...
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    market: Mapped[str | None] = mapped_column(String(10))

    coverage: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    frequency: Mapped[str | None] = mapped_column(String(30))
    requires_auth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Rate limiting configuration, enforced centrally by RateLimiter.
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    rate_limit_per_day: Mapped[int | None] = mapped_column(Integer)
    max_concurrency: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    min_interval_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    timeout_seconds: Mapped[float] = mapped_column(
        Numeric(6, 2), nullable=False, default=20
    )
    max_retries: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=SourceStatus.ACTIVE)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    licence: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE','DEGRADED','DISABLED','UNVERIFIED')", name="status_allowed"
        ),
        CheckConstraint("rate_limit_per_minute > 0", name="rpm_positive"),
        CheckConstraint("max_concurrency > 0", name="concurrency_positive"),
    )


# --------------------------------------------------------------------------
class RawIngestion(Base):
    """One row per provider call. The provenance anchor for everything else.

    Canonical rows carry `ingestion_id` pointing here, so any stored value leads
    back to the exact request that produced it, when it was made, how long it
    took, and a hash of the response body.
    """

    __tablename__ = "raw_ingestions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    dataset: Mapped[str] = mapped_column(String(50), nullable=False)
    params: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    transport: Mapped[str] = mapped_column(String(10), nullable=False, default=Transport.LIVE)

    # --- the four timestamps that answer "when" ---------------------------
    source_request_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # What trading day the payload represents — from the payload, never from now().
    data_as_of: Mapped[date | None] = mapped_column(Date)

    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    response_bytes: Mapped[int | None] = mapped_column(Integer)
    response_hash: Mapped[str | None] = mapped_column(String(64))
    storage_uri: Mapped[str | None] = mapped_column(String(500))

    record_count: Mapped[int | None] = mapped_column(Integer)
    accepted_count: Mapped[int | None] = mapped_column(Integer)
    quarantined_count: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="SUCCESS")
    error: Mapped[str | None] = mapped_column(Text)
    job_run_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint("transport IN ('LIVE','REPLAY','MOCK')", name="transport_allowed"),
        CheckConstraint(
            "status IN ('SUCCESS','PARTIAL','FAILED')", name="ingestion_status_allowed"
        ),
        Index("ix_raw_ingestions_dataset", "dataset", "source_request_at"),
        Index("ix_raw_ingestions_as_of", "dataset", "data_as_of"),
        Index("ix_raw_ingestions_hash", "response_hash"),
    )


# --------------------------------------------------------------------------
class DataQuarantine(Base):
    """Records that failed validation.

    Nothing is discarded silently. A quarantined row keeps the raw payload, the
    reason, and the ingestion that produced it, so a source changing its format
    shows up as a spike here rather than as missing data nobody noticed.
    """

    __tablename__ = "data_quarantine"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    dataset: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20))
    trading_date: Mapped[date | None] = mapped_column(Date)

    raw_record: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    normalized_record: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    rule_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    errors: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="FATAL")

    ingestion_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(30))
    resolution_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("severity IN ('FATAL','WARN')", name="severity_allowed"),
        CheckConstraint(
            "resolution IS NULL OR resolution IN "
            "('ACCEPTED','DISCARDED','SOURCE_FIXED','REINGESTED')",
            name="resolution_allowed",
        ),
        Index("ix_quarantine_dataset", "dataset", "created_at"),
        Index("ix_quarantine_review", "reviewed_at", "dataset"),
    )


# --------------------------------------------------------------------------
class DataFreshness(Base):
    """Freshness contract and current state, one row per dataset.

    `expected_lag_minutes` is measured from the close of the trading day the data
    describes — not from wall-clock now — so the check is meaningful across
    weekends and holidays.
    """

    __tablename__ = "data_freshness"

    dataset: Mapped[str] = mapped_column(String(50), primary_key=True)
    market: Mapped[str | None] = mapped_column(String(10))
    description: Mapped[str | None] = mapped_column(String(200))

    expected_lag_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_frequency: Mapped[str] = mapped_column(
        String(20), nullable=False, default="TRADING_DAY"
    )

    last_data_date: Mapped[date | None] = mapped_column(Date)
    last_ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_next_update: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    record_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FreshnessStatus.MISSING
    )
    lag_minutes: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('FRESH','STALE','MISSING','DEGRADED')", name="freshness_status_allowed"
        ),
        CheckConstraint("expected_lag_minutes > 0", name="expected_lag_positive"),
    )


# --------------------------------------------------------------------------
class BackfillCheckpoint(Base):
    """Resumable backfill state.

    A backfill that has to restart from the beginning after an interruption is
    not a backfill, it is a denial-of-service against the exchange. The cursor is
    committed after every batch.
    """

    __tablename__ = "backfill_checkpoints"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    job_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    dataset: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[str] = mapped_column(String(40), nullable=False, default="ALL")

    range_start: Mapped[date] = mapped_column(Date, nullable=False)
    range_end: Mapped[date] = mapped_column(Date, nullable=False)
    cursor: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=BackfillStatus.PENDING)

    units_total: Mapped[int | None] = mapped_column(Integer)
    units_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    units_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    units_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_written: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    records_quarantined: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    last_error: Mapped[str | None] = mapped_column(Text)
    failed_units: Mapped[list[str] | None] = mapped_column(JSONB)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','PAUSED','COMPLETED','FAILED')",
            name="backfill_status_allowed",
        ),
        CheckConstraint("range_end >= range_start", name="range_ordered"),
        CheckConstraint(
            "cursor >= range_start AND cursor <= range_end + 1", name="cursor_within_range"
        ),
        Index("ix_backfill_dataset", "dataset", "status"),
    )

    @property
    def progress(self) -> float:
        if not self.units_total:
            return 0.0
        return min(1.0, self.units_done / self.units_total)


# --------------------------------------------------------------------------
class IngestionMetric(Base):
    """Per-stage timings. Collected before optimising anything.

    ARCHITECTURE.md §16 says measure then scale; this is the measuring. Nothing
    reads these yet except the Data Operations dashboard.
    """

    __tablename__ = "ingestion_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    dataset: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    trading_date: Mapped[date | None] = mapped_column(Date)

    provider_ms: Mapped[int | None] = mapped_column(Integer)
    parse_ms: Mapped[int | None] = mapped_column(Integer)
    validation_ms: Mapped[int | None] = mapped_column(Integer)
    persist_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int | None] = mapped_column(Integer)

    records_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_quarantined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_per_second: Mapped[float | None] = mapped_column(Numeric(12, 2))

    ingestion_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_ingestion_metrics_dataset", "dataset", "created_at"),)


__all__ = [
    "BackfillCheckpoint",
    "BackfillStatus",
    "DataFreshness",
    "DataQuarantine",
    "DataSource",
    "FreshnessStatus",
    "IngestionMetric",
    "RawIngestion",
    "SourceStatus",
    "Transport",
]
