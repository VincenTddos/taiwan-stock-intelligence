"""Worker tests.

Two layers, deliberately:

1. Tests that run the task function directly — these always run in CI and
   verify the task's own logic (it must report component failures rather than
   raise).
2. Tests marked `worker` that require a *live* worker and prove the full
   API → Redis → Celery → Task round trip. They skip with a clear message when
   no worker is listening, so a developer never sees a green suite that
   silently skipped the thing they were trying to check.
"""

from __future__ import annotations

import pytest

from app.workers.celery_app import celery_app
from app.workers.tasks.health import health_check_task, write_heartbeat

pytestmark = pytest.mark.integration


def _worker_online() -> bool:
    try:
        return bool(celery_app.control.ping(timeout=1.5))
    except Exception:
        return False


# ---------------------------------------------------------------- config
def test_broker_and_backend_are_separate_databases():
    assert celery_app.conf.broker_url != celery_app.conf.result_backend


def test_queue_topology_declared():
    routes = celery_app.conf.task_routes
    assert {r["queue"] for r in routes.values()} == {
        "q_ingest",
        "q_compute",
        "q_nlp",
        "q_user",
        "q_maint",
    }


def test_reliability_settings():
    """acks_late + reject_on_worker_lost is what makes a killed worker
    redeliver its task instead of silently losing it."""
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_health_task_is_registered():
    assert "maint.health_check" in celery_app.tasks
    assert "maint.heartbeat" in celery_app.tasks


# ------------------------------------------------------------ direct call
async def test_health_check_task_runs_inline(db_schema, redis_client):
    import asyncio

    result = await asyncio.to_thread(health_check_task.apply(kwargs={"echo": "unit"}).get)
    assert result["echo"] == "unit"
    assert result["status"] in ("healthy", "degraded")
    assert set(result["components"]) == {"redis", "postgres"}
    assert result["duration_ms"] >= 0


async def test_health_check_task_reports_rather_than_raises(monkeypatch, redis_client):
    """If Postgres is down the task must still return a report — the caller
    needs the diagnosis, not a traceback."""
    import asyncio

    import app.workers.tasks.health as mod

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("simulated database outage")

    monkeypatch.setattr(mod, "create_engine", _boom)
    result = await asyncio.to_thread(health_check_task.apply(kwargs={"echo": "x"}).get)
    assert result["status"] == "degraded"
    assert result["components"]["postgres"]["status"] == "unhealthy"
    assert "simulated database outage" in result["components"]["postgres"]["error"]


async def test_heartbeat_written_with_ttl(redis_client):
    import asyncio

    from app.services.health_service import WORKER_HEARTBEAT_KEY

    stamp = await asyncio.to_thread(write_heartbeat)
    assert stamp
    assert await redis_client.get(WORKER_HEARTBEAT_KEY) == stamp
    assert await redis_client.ttl(WORKER_HEARTBEAT_KEY) > 0


# ------------------------------------------------------- live round trip
@pytest.mark.worker
def test_end_to_end_dispatch_through_broker():
    if not _worker_online():
        pytest.skip("no live Celery worker (start one with: make worker)")

    async_result = health_check_task.apply_async(kwargs={"echo": "e2e"}, queue="q_maint")
    payload = async_result.get(timeout=20)

    assert payload["echo"] == "e2e"
    assert payload["task_id"] == async_result.id
    assert payload["components"]["redis"]["status"] == "healthy"
    assert payload["components"]["postgres"]["status"] == "healthy"
