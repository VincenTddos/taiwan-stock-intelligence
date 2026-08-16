# ADR-002: Celery with a Redis broker, rather than RQ or Dramatiq

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

Background work uses Celery, with Redis as both broker and result backend.

## Rationale

Celery's beat scheduler is mature, Flower gives usable queue observability for free, and the ecosystem is large enough that operational questions have answers. RQ and Dramatiq are simpler but would need a separate scheduler and offer weaker introspection.

Redis is already required for caching, so the broker adds no new service.

## Revisit when

The job count drops below roughly five and the scheduling needs stay trivial, at which point APScheduler in-process would be less machinery.
