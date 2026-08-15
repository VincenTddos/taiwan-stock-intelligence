"""Health checks.

Design notes:

* Every check is independently timed and independently failable. One broken
  component must not turn the whole report into a 500 — a health endpoint that
  crashes when something is unhealthy is useless precisely when you need it.
* Checks run concurrently with a per-check timeout, so `/health/full` is bounded
  by the slowest single check rather than their sum.
* Extension checks (TimescaleDB, pgvector) are separate components, because
  "Postgres is up" and "Postgres has the extensions this platform requires" are
  different failures with different fixes.
* Worker liveness is read from a Redis heartbeat written by the Celery worker,
  plus a Celery control ping. The heartbeat alone would go stale silently; the
  ping alone is slow and fails during transient broker hiccups.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_redis
from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.health import ComponentHealth, HealthReport, HealthStatus

log = get_logger(__name__)

WORKER_HEARTBEAT_KEY = "worker:heartbeat"

# Components whose failure makes the whole system unhealthy. The LLM is
# deliberately absent: ADR-011 requires core to run without it.
REQUIRED_COMPONENTS = {"api", "postgres", "redis", "celery"}


def _now() -> datetime:
    return datetime.now(UTC)


async def _timed(name: str, coro_factory: Any, timeout: float) -> ComponentHealth:
    start = time.perf_counter()
    try:
        detail, version = await asyncio.wait_for(coro_factory(), timeout=timeout)
        return ComponentHealth(
            name=name,
            status=HealthStatus.HEALTHY,
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
            version=version,
            checked_at=_now(),
            detail=detail,
        )
    except TimeoutError:
        return ComponentHealth(
            name=name,
            status=HealthStatus.UNHEALTHY,
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
            checked_at=_now(),
            error=f"timeout after {timeout}s",
        )
    except Exception as exc:
        log.warning("health_check_failed", component=name, error=str(exc))
        return ComponentHealth(
            name=name,
            status=HealthStatus.UNHEALTHY,
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
            checked_at=_now(),
            error=str(exc)[:300],
        )


class HealthService:
    def __init__(self, settings: Settings, session: AsyncSession | None = None) -> None:
        self.settings = settings
        self.session = session
        self.timeout = settings.HEALTH_TIMEOUT_SECONDS

    # ------------------------------------------------------------ postgres
    async def check_postgres(self) -> ComponentHealth:
        async def probe() -> tuple[dict[str, Any], str | None]:
            assert self.session is not None, "database check requires a session"
            await self.session.execute(text("SELECT 1"))
            version = (await self.session.execute(text("SHOW server_version"))).scalar_one()
            db = (await self.session.execute(text("SELECT current_database()"))).scalar_one()
            return {"database": db}, str(version)

        return await _timed("postgres", probe, self.timeout)

    async def _check_extension(self, name: str, component: str, required: bool) -> ComponentHealth:
        async def probe() -> tuple[dict[str, Any], str | None]:
            assert self.session is not None
            row = (
                await self.session.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = :n"),
                    {"n": name},
                )
            ).scalar_one_or_none()
            if row is None:
                raise RuntimeError(f"extension '{name}' is not installed in this database")
            return {"extension": name}, str(row)

        result = await _timed(component, probe, self.timeout)
        if result.status is HealthStatus.UNHEALTHY and not required:
            # Not required in this environment (local dev on stock Postgres):
            # report it honestly as degraded rather than failing the system.
            return result.model_copy(
                update={
                    "status": HealthStatus.DEGRADED,
                    "detail": {"required": False, "note": "not required in this environment"},
                }
            )
        return result

    async def check_timescaledb(self) -> ComponentHealth:
        return await self._check_extension(
            "timescaledb", "timescaledb", self.settings.REQUIRE_TIMESCALEDB
        )

    async def check_pgvector(self) -> ComponentHealth:
        return await self._check_extension("vector", "pgvector", self.settings.REQUIRE_PGVECTOR)

    # --------------------------------------------------------------- redis
    async def check_redis(self) -> ComponentHealth:
        async def probe() -> tuple[dict[str, Any], str | None]:
            redis = get_redis(self.settings)
            await redis.ping()
            info = await redis.info("server")
            mem = await redis.info("memory")
            return (
                {
                    "used_memory_human": mem.get("used_memory_human"),
                    "cache_db": self.settings.REDIS_CACHE_DB,
                    "broker_db": self.settings.REDIS_BROKER_DB,
                },
                str(info.get("redis_version")),
            )

        return await _timed("redis", probe, self.timeout)

    # -------------------------------------------------------------- worker
    async def check_worker(self) -> ComponentHealth:
        async def probe() -> tuple[dict[str, Any], str | None]:
            redis = get_redis(self.settings)
            raw = await redis.get(WORKER_HEARTBEAT_KEY)
            heartbeat = raw.decode() if isinstance(raw, bytes) else raw

            detail: dict[str, Any] = {"heartbeat": heartbeat}
            if heartbeat:
                age = (_now() - datetime.fromisoformat(heartbeat)).total_seconds()
                detail["heartbeat_age_seconds"] = round(age, 1)
                if age <= self.settings.WORKER_HEARTBEAT_TTL_SECONDS:
                    detail["method"] = "heartbeat"
                    return detail, None

            # Heartbeat missing or stale — ask the broker directly.
            workers = await asyncio.to_thread(self._ping_workers)
            if not workers:
                raise RuntimeError("no Celery worker responded to ping")
            detail["method"] = "control_ping"
            detail["workers"] = workers
            return detail, None

        return await _timed("celery", probe, self.timeout)

    def _ping_workers(self) -> list[str]:
        from app.workers.celery_app import celery_app

        replies = celery_app.control.ping(timeout=self.timeout * 0.6) or []
        return [name for reply in replies for name in reply]

    # ----------------------------------------------------------------- llm
    async def check_llm(self) -> ComponentHealth:
        if not self.settings.ENABLE_LLM:
            return ComponentHealth(
                name="llm",
                status=HealthStatus.DISABLED,
                checked_at=_now(),
                detail={
                    "reason": "ENABLE_LLM=false",
                    "note": "core (market data, quant, api, backtest) does not depend on the LLM",
                },
            )

        async def probe() -> tuple[dict[str, Any], str | None]:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.settings.OLLAMA_BASE_URL}/api/version")
                resp.raise_for_status()
                payload = resp.json()
            return {"provider": "ollama"}, str(payload.get("version"))

        return await _timed("llm", probe, self.timeout)

    # ------------------------------------------------------------- reports
    def api_component(self) -> ComponentHealth:
        return ComponentHealth(
            name="api",
            status=HealthStatus.HEALTHY,
            version=self.settings.APP_VERSION,
            checked_at=_now(),
            detail={"environment": self.settings.APP_ENV.value},
        )

    async def db_components(self) -> list[ComponentHealth]:
        """Database checks run sequentially on purpose.

        A single AsyncSession multiplexes one connection; issuing concurrent
        statements on it raises `IllegalStateChangeError`, which would show up
        as three spurious "unhealthy" components. These probes are sub-millisecond,
        so serialising them costs nothing.
        """
        if self.session is None:
            return []
        return [
            await self.check_postgres(),
            await self.check_timescaledb(),
            await self.check_pgvector(),
        ]

    async def full_report(self) -> HealthReport:
        # Non-DB checks are independent and slow (network), so they run together.
        independent = await asyncio.gather(
            self.check_redis(), self.check_worker(), self.check_llm()
        )
        components = [self.api_component(), *await self.db_components(), *independent]
        return self.build_report(components)

    def build_report(self, components: list[ComponentHealth]) -> HealthReport:
        status = HealthStatus.HEALTHY
        for c in components:
            if c.status is HealthStatus.DISABLED:
                continue
            if c.name in REQUIRED_COMPONENTS and c.status is HealthStatus.UNHEALTHY:
                status = HealthStatus.UNHEALTHY
                break
            if c.status in (HealthStatus.UNHEALTHY, HealthStatus.DEGRADED):
                status = HealthStatus.DEGRADED

        return HealthReport(
            status=status,
            app=self.settings.APP_NAME,
            version=self.settings.APP_VERSION,
            environment=self.settings.APP_ENV.value,
            checked_at=_now(),
            components=components,
        )
