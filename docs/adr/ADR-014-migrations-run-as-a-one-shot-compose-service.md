# ADR-014: Migrations run as a one-shot compose service

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 1 — Foundation |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

`alembic upgrade head` runs in a dedicated `migrate` service. `api` and `worker` declare `depends_on: migrate: {condition: service_completed_successfully}`.

## Context

The common alternatives are:

1. Run migrations in the application's entrypoint, before starting the server
2. Run them manually before `docker compose up`
3. Run them as a separate one-shot service (chosen)

## Rationale

Option 1 has a race: with more than one API replica, or with the API and the worker starting together, two processes attempt the same migration simultaneously. Alembic's version table gives some protection, but partially-applied DDL under contention is a genuinely bad place to be.

Option 2 relies on a human remembering. That works until the day it does not, and the failure is an application running against a schema it does not understand — which surfaces as confusing runtime errors rather than a clear startup failure.

Option 3 makes the ordering **structural**. Compose will not start the API until the migration container has exited zero. If a migration fails, nothing starts, and the error is the first thing in the logs rather than the tenth.

## Consequences

### Positive

- No process can start against a stale schema
- No concurrent-migration race
- Migration failure is loud, early, and unambiguous
- The same mechanism works in CI without special handling

### Negative / accepted cost

- Adds one container to the compose file
- Startup is serialised: nothing runs until migrations finish

## Revisit when

Zero-downtime deployment becomes a requirement. That needs expand/contract migrations coordinated with a rolling restart, which is a different mechanism — this decision would then be replaced rather than tuned.
