from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

HEALTH_ENDPOINTS = [
    "/api/v1/health",
    "/api/v1/health/database",
    "/api/v1/health/redis",
    "/api/v1/health/worker",
    "/api/v1/health/full",
]


@pytest.mark.parametrize("path", HEALTH_ENDPOINTS)
async def test_health_endpoints_never_500(client, path):
    """A health endpoint that crashes is useless exactly when it matters.
    Any status other than 200/503 means the handler itself broke."""
    resp = await client.get(path)
    assert resp.status_code in (200, 503), resp.text


@pytest.mark.parametrize("path", HEALTH_ENDPOINTS)
async def test_health_endpoints_return_envelope(client, path):
    body = (await client.get(path)).json()
    assert "data" in body and "meta" in body
    assert body["meta"]["request_id"] is not None
    assert body["meta"]["is_demo"] is False


async def test_health_reports_postgres_and_redis_healthy(client):
    body = (await client.get("/api/v1/health")).json()["data"]
    services = {c["name"]: c["status"] for c in body["components"]}
    assert services["api"] == "healthy"
    assert services["postgres"] == "healthy"
    assert services["redis"] == "healthy"


async def test_database_health_reports_extensions_individually(client):
    components = (await client.get("/api/v1/health/database")).json()["data"]
    names = {c["name"] for c in components}
    assert names == {"postgres", "timescaledb", "pgvector"}

    pgvector = next(c for c in components if c["name"] == "pgvector")
    assert pgvector["status"] == "healthy", "pgvector is a hard requirement"
    assert pgvector["version"] is not None


async def test_llm_disabled_does_not_degrade_the_system(client):
    """ADR-011: core must be healthy with the LLM switched off."""
    report = (await client.get("/api/v1/health/full")).json()["data"]
    llm = next(c for c in report["components"] if c["name"] == "llm")
    assert llm["status"] == "disabled"

    non_llm = [c for c in report["components"] if c["name"] not in ("llm", "celery")]
    assert all(c["status"] in ("healthy", "degraded") for c in non_llm)


async def test_unhealthy_worker_does_not_break_the_report(client):
    """With no Celery worker running, /health must still answer — reporting the
    worker as unhealthy rather than failing to respond at all."""
    resp = await client.get("/api/v1/health/worker")
    assert resp.status_code in (200, 503)
    assert resp.json()["data"]["name"] == "celery"


async def test_response_carries_request_id_header(client):
    resp = await client.get("/api/v1/health")
    assert resp.headers["X-Request-ID"]
    assert resp.headers["X-Request-ID"] == resp.json()["meta"]["request_id"]


async def test_request_id_is_echoed_when_supplied(client):
    resp = await client.get("/api/v1/health", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["X-Request-ID"] == "abc-123"


async def test_security_headers_present(client):
    resp = await client.get("/api/v1/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
