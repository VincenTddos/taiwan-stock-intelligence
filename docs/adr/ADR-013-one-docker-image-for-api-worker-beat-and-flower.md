# ADR-013: One Docker image for API, worker, beat and Flower

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 1 — Foundation |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

The API, the Celery worker, the Celery beat scheduler and Flower all run from a **single backend image**. They differ only in the command the container is started with.

## Context

These four processes share the entire backend package: the same SQLAlchemy models, the same Pydantic contracts, the same configuration validation, the same providers. They differ only in entry point.

The alternative is a separate image per process, each with a trimmed dependency set.

## Rationale

The failure mode we are avoiding is **code drift between processes**. If the API and the worker are built from separate images, it becomes possible to deploy a worker whose idea of `DailyPrice` differs from the API's. That class of bug is silent, intermittent, and extremely expensive to diagnose — the data looks almost right.

One image makes that impossible by construction: if the API has a model, the worker has exactly the same model, byte for byte.

Fault isolation, which is the usual argument for separate images, is preserved anyway — they are still separate *containers*. A worker that exhausts memory does not take the API with it.

The cost is image size: the API carries Celery it does not use, and the worker carries Uvicorn it does not use. On a single-machine deployment sharing one layer cache, that cost is close to zero.

## Consequences

### Positive

- Deployed code is provably identical across processes
- One build, one cache, one vulnerability surface to patch
- A developer changes a model once and every process sees it
- Fault domains remain separate (separate containers)

### Negative / accepted cost

- Each image carries dependencies it does not use
- A change to any dependency rebuilds the image for every process

## Revisit when

Dependency sets diverge enough that image size or build time becomes a real constraint — for example when the ML worker pulls in a multi-gigabyte inference stack that the API has no use for. At that point split *that* worker out, not everything.
