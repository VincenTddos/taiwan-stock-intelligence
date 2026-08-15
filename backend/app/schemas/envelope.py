"""The single response shape used by every endpoint.

`meta` is not optional decoration — it is the enforcement point for the rule
that no number is ever shown without its timestamp, source, model version and
demo flag (API_SPEC.md §1.2). A test asserts every v1 route returns it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class CacheMeta(BaseModel):
    hit: bool = False
    age_seconds: int | None = None


class QualityMeta(BaseModel):
    overall: float | None = None
    freshness: float | None = None
    completeness: float | None = None
    consistency: float | None = None
    source_quality: float | None = None


class Meta(BaseModel):
    """Provenance for the payload. Phase 1 populates what exists today."""

    model_config = ConfigDict(protected_namespaces=())

    data_timestamp: datetime | None = None
    trading_date: date | None = None
    source: list[str] = Field(default_factory=list)

    # --- reproducibility triple (Phase 1 establishes the contract) ---
    model_version: str | None = None
    feature_version: str | None = None
    dataset_version: str | None = None
    calculated_at: datetime | None = None
    data_as_of: date | None = None

    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    # --- honesty flags ---
    is_demo: bool = False
    is_stale: bool = False

    quality: QualityMeta | None = None
    cache: CacheMeta = Field(default_factory=CacheMeta)
    request_id: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Pagination(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    next_cursor: str | None = None


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta = Field(default_factory=Meta)
    pagination: Pagination | None = None


def envelope(data: T, meta: Meta | None = None, **meta_kwargs: object) -> Envelope[T]:
    """Convenience builder that always attaches the current request_id."""
    from app.core.logging import request_id_var

    m = meta or Meta(**meta_kwargs)
    if m.request_id is None:
        m.request_id = request_id_var.get()
    return Envelope[T](data=data, meta=m)
