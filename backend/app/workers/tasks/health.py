"""Phase 1 worker tasks: prove the API → Redis → Celery → Task path end to end.

`health_check_task` deliberately touches both Postgres and Redis from inside the
worker process. A worker that can reach the broker but not the database is a
failure mode that a pure `ping` would report as healthy.
"""

from __future__ import annotations

import os
import socket
import time
from datetime import UTC, datetime
from typing import Any

import redis
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.health_service import WORKER_HEARTBEAT_KEY
from app.workers.celery_app import celery_app

log = get_logger(__name__)
settings = get_settings()


def _sync_redis() -> redis.Redis:
    return redis.from_url(settings.redis_cache_url, decode_responses=True)


def write_heartbeat() -> str:
    """Record worker liveness. TTL is 2x the beat interval so a dead worker
    disappears from the health page on its own, without a reaper."""
    stamp = datetime.now(UTC).isoformat()
    client = _sync_redis()
    try:
        client.setex(WORKER_HEARTBEAT_KEY, settings.WORKER_HEARTBEAT_TTL_SECONDS, stamp)
    finally:
        client.close()
    return stamp


@celery_app.task(name="maint.heartbeat", ignore_result=True)
def heartbeat_task() -> str:
    return write_heartbeat()


@celery_app.task(name="maint.health_check", bind=True, max_retries=2, soft_time_limit=20)
def health_check_task(self: Any, echo: str | None = None) -> dict[str, Any]:
    """Round-trip check. Returns a structured report, never raises for a
    component being down — the caller needs the detail, not a stack trace."""
    started = time.perf_counter()
    report: dict[str, Any] = {
        "task_id": self.request.id,
        "worker": f"{socket.gethostname()}:{os.getpid()}",
        "echo": echo,
        "checked_at": datetime.now(UTC).isoformat(),
        "components": {},
    }

    # --- redis -------------------------------------------------------------
    t0 = time.perf_counter()
    try:
        client = _sync_redis()
        client.ping()
        client.close()
        report["components"]["redis"] = {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    except Exception as exc:
        report["components"]["redis"] = {"status": "unhealthy", "error": str(exc)[:200]}

    # --- postgres ----------------------------------------------------------
    t0 = time.perf_counter()
    engine = None
    try:
        # Celery tasks are synchronous; use the psycopg driver rather than asyncpg.
        engine = create_engine(
            settings.database_url.replace("+asyncpg", "+psycopg"), pool_pre_ping=True, future=True
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            version = conn.execute(text("SHOW server_version")).scalar_one()
        report["components"]["postgres"] = {
            "status": "healthy",
            "version": str(version),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        }
    except Exception as exc:
        report["components"]["postgres"] = {"status": "unhealthy", "error": str(exc)[:200]}
    finally:
        if engine is not None:
            engine.dispose()

    report["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    report["status"] = (
        "healthy"
        if all(c.get("status") == "healthy" for c in report["components"].values())
        else "degraded"
    )
    write_heartbeat()
    log.info(
        "health_check_task_completed",
        status=report["status"],
        duration_ms=report["duration_ms"],
    )
    return report
