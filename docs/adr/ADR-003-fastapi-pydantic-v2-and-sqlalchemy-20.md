# ADR-003: FastAPI, Pydantic v2 and SQLAlchemy 2.0

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

The backend stack is FastAPI + Pydantic v2 + SQLAlchemy 2.0 (async) + Alembic.

## Rationale

Pydantic v2 gives runtime validation and static types from one declaration, and FastAPI turns those into an OpenAPI schema automatically — which is what lets the frontend generate its types instead of hand-copying them. SQLAlchemy 2.0's typed API is a genuine improvement over 1.x for a codebase running mypy strict.

Async throughout matters because ingestion is IO-bound: dozens of concurrent HTTP requests to exchange endpoints.

## Revisit when

No foreseeable condition.
