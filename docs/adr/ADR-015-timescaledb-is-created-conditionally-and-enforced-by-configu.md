# ADR-015: TimescaleDB is created conditionally and enforced by configuration

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 1 — Foundation |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

Migration `0001` creates the `timescaledb` extension **only if the server offers it**, logging a clear message otherwise. Its presence is made mandatory in staging and production by the `REQUIRE_TIMESCALEDB` setting, and reported independently by `GET /api/v1/health/database`.

## Context

`docker compose` uses `timescale/timescaledb-ha:pg16`, which ships the extension. But a developer running a stock `apt install postgresql-16` — which is exactly what happened in the Phase 1 build environment — does not have it, and `CREATE EXTENSION timescaledb` fails outright.

An unconditional `CREATE EXTENSION` makes the entire migration chain unrunnable on such a machine. Hard-coding it as optional everywhere loses the guarantee where it matters.

## Rationale

These are two different questions and they deserve two different mechanisms:

- *Can this migration run here?* — answered by probing `pg_available_extensions`
- *Should this deployment have TimescaleDB at all?* — answered by configuration and enforced at boot and in the health report

Separating them means local development stays frictionless while staging and production still refuse to run without it. `Settings._check_consistency` rejects `REQUIRE_TIMESCALEDB=false` outside local/test, so the escape hatch cannot be left open by accident.

The health endpoint reports the extension as a **separate component**, because "Postgres is up" and "Postgres has what this platform needs" are different failures with different fixes. In the Phase 1 sandbox this surfaced honestly as `TIMESCALEDB ● DEGRADED — extension 'timescaledb' is not installed in this database` rather than as a silent assumption.

## Consequences

### Positive

- Local development works on a plain Postgres install
- Staging and production cannot start without the extension
- The gap is visible in the health report rather than discovered at query time
- Migrations stay runnable everywhere, which keeps CI simple

### Negative / accepted cost

- Two places must agree (the migration probe and the setting)
- A developer on plain Postgres cannot exercise hypertable behaviour locally; those tests skip with an explicit message

## Revisit when

Never, while the platform supports both container and bare-metal development. If the project standardises on containers only, the probe can be dropped and the extension created unconditionally.
