# ADR-018: Database health probes run sequentially

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 1 — Foundation |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

Within `HealthService`, the Postgres / TimescaleDB / pgvector probes execute **sequentially** on the shared `AsyncSession`. Only the independent probes (Redis, Celery, LLM) run concurrently via `asyncio.gather`.

## Context

`/health/full` originally gathered every probe concurrently, which is the obvious way to keep the endpoint fast.

## Rationale

A single `AsyncSession` multiplexes one database connection. Issuing concurrent statements on it raises `IllegalStateChangeError: This session is provisioning a new connection; concurrent operations are not permitted`.

Because each probe catches its own exceptions — by design, so that one broken component cannot crash the endpoint — the error did not surface as a crash. It surfaced as **three components reporting themselves unhealthy at once**, with an error message about session state that has nothing to do with database health.

That is worse than a crash: a health endpoint that lies is actively harmful. It would have triggered a false alarm every single time `/health/full` was called.

The fix is trivial and free. These probes are `SELECT 1`-class queries taking under a millisecond; serialising three of them costs nothing measurable. The concurrency was never buying anything.

Non-database probes still run together, because those genuinely are independent and genuinely are slow (network round trips).

## Consequences

### Positive

- The health report tells the truth
- No measurable latency cost (sub-millisecond probes)
- Network-bound probes still overlap, so the endpoint stays fast

### Negative / accepted cost

- A future contributor may "optimise" this back without reading the comment explaining it

## Revisit when

Each probe is given its own session or connection from the pool. Then concurrency is safe — but it buys almost nothing, so there is little reason to bother.
