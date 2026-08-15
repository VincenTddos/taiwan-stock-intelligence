from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Response, status

from app.api.deps import SessionDep, SettingsDep, require_role
from app.schemas.envelope import Envelope, envelope
from app.schemas.health import ComponentHealth, HealthReport, HealthStatus
from app.services.health_service import HealthService

router = APIRouter(prefix="/health", tags=["health"])

# 503 when unhealthy so that container orchestrators and uptime checks can act
# on the status code without parsing the body.
_STATUS_CODE = {
    HealthStatus.HEALTHY: status.HTTP_200_OK,
    HealthStatus.DEGRADED: status.HTTP_200_OK,
    HealthStatus.UNHEALTHY: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _apply_code(response: Response, report: HealthReport) -> None:
    response.status_code = _STATUS_CODE.get(report.status, status.HTTP_200_OK)


@router.get("", response_model=Envelope[HealthReport], summary="Liveness + core dependencies")
async def health(
    response: Response, settings: SettingsDep, session: SessionDep
) -> Envelope[HealthReport]:
    service = HealthService(settings, session)
    components = [
        service.api_component(),
        await service.check_postgres(),
        await service.check_redis(),
        await service.check_worker(),
    ]
    report = service.build_report(components)
    _apply_code(response, report)
    return envelope(report, source=["SELF"])


@router.get("/database", response_model=Envelope[list[ComponentHealth]])
async def health_database(
    response: Response, settings: SettingsDep, session: SessionDep
) -> Envelope[list[ComponentHealth]]:
    service = HealthService(settings, session)
    components = await service.db_components()
    if any(c.status is HealthStatus.UNHEALTHY for c in components):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return envelope(components, source=["SELF"])


@router.get("/redis", response_model=Envelope[ComponentHealth])
async def health_redis(response: Response, settings: SettingsDep) -> Envelope[ComponentHealth]:
    component = await HealthService(settings).check_redis()
    if component.status is HealthStatus.UNHEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return envelope(component, source=["SELF"])


@router.get("/worker", response_model=Envelope[ComponentHealth])
async def health_worker(response: Response, settings: SettingsDep) -> Envelope[ComponentHealth]:
    component = await HealthService(settings).check_worker()
    if component.status is HealthStatus.UNHEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return envelope(component, source=["SELF"])


@router.get("/full", response_model=Envelope[HealthReport], summary="Every component")
async def health_full(
    response: Response, settings: SettingsDep, session: SessionDep
) -> Envelope[HealthReport]:
    report = await HealthService(settings, session).full_report()
    _apply_code(response, report)
    return envelope(report, source=["SELF"])


@router.post(
    "/worker/echo",
    response_model=Envelope[dict[str, object]],
    summary="Dispatch health_check_task and wait for the worker's reply",
)
async def worker_echo(
    settings: SettingsDep,
    _: Annotated[object, require_role("admin")],
    message: str = "ping",
    timeout: float = 10.0,
) -> Envelope[dict[str, object]]:
    """Proves the full API → Redis → Celery → Task path in one call.

    Admin-only: it consumes a worker slot, so it must not be publicly triggerable.
    """
    import asyncio

    from app.workers.tasks.health import health_check_task

    async_result = health_check_task.apply_async(kwargs={"echo": message}, queue="q_maint")
    try:
        payload = await asyncio.wait_for(
            asyncio.to_thread(async_result.get, timeout=timeout), timeout=timeout + 2
        )
    except Exception as exc:
        return envelope(
            {
                "dispatched": True,
                "task_id": async_result.id,
                "completed": False,
                "error": str(exc)[:300],
            },
            source=["SELF"],
        )
    return envelope(
        {"dispatched": True, "task_id": async_result.id, "completed": True, "result": payload},
        source=["SELF"],
    )
