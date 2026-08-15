from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class ComponentHealth(BaseModel):
    name: str
    status: HealthStatus
    latency_ms: float | None = None
    version: str | None = None
    checked_at: datetime
    detail: dict[str, object] = Field(default_factory=dict)
    error: str | None = None


class HealthReport(BaseModel):
    """Aggregate report.

    `status` is the worst status among *required* components. An optional
    component that is switched off (e.g. the LLM) reports `disabled` and does
    not drag the system to degraded — that distinction is what makes the health
    page actionable instead of permanently amber.
    """

    status: HealthStatus
    app: str
    version: str
    environment: str
    checked_at: datetime
    components: list[ComponentHealth] = Field(default_factory=list)

    @property
    def services(self) -> dict[str, str]:
        return {c.name: c.status.value for c in self.components}
