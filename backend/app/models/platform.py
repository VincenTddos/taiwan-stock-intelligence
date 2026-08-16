"""Platform tables: audit trail, component health, job runs.

These exist in Phase 1 (rather than Phase 2) because retro-fitting an audit
trail after the fact means the earliest and most interesting events are gone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(80))
    request_id: Mapped[str | None] = mapped_column(String(50))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[str] = mapped_column(String(20), nullable=False)  # SUCCESS/DENIED/ERROR
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class SystemHealth(Base):
    """Last known state of each component, written by health checks and jobs."""

    __tablename__ = "system_health"

    component: Mapped[str] = mapped_column(String(50), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class JobRun(Base):
    """One row per background job execution. Phase 1 seeds it with health_check_task."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(80), index=True)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    records_in: Mapped[int | None] = mapped_column(Integer)
    records_out: Mapped[int | None] = mapped_column(Integer)
    records_rejected: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(2000))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker: Mapped[str | None] = mapped_column(String(80))
