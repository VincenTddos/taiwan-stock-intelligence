# ADR-016: Cache namespace versions start at zero

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 1 — Foundation |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

`cache_version(namespace)` returns **0** when the counter key does not exist, not 1.

## Context

Cache invalidation uses a version prefix: keys are `v{n}:{namespace}:{key}`, and invalidating a namespace means incrementing `cache_version:{namespace}` so that every subsequent lookup misses. This avoids `KEYS`/`SCAN` sweeps entirely.

The first implementation returned 1 for a missing counter, which reads naturally — "version one" — and is wrong.

## Rationale

Redis `INCR` on a missing key sets it to 1. So with a default of 1, the very first invalidation moves the version from 1 to 1: **the first `bump_cache_version` call is a no-op**, and every entry written before it survives an invalidation that appeared to succeed.

That is a nasty failure. It only affects the first invalidation of each namespace, so it would pass casual testing and then, in production, serve one generation of stale prices after the first ingestion run — precisely when correctness matters most.

Starting at 0 makes the first `INCR` move 0 → 1, so the first invalidation actually invalidates.

This was found by `test_bumping_version_retires_a_namespace`, which asserted that the key changes after a bump. It is documented here because the correct value is counter-intuitive and someone will eventually be tempted to "fix" it back.

## Consequences

### Positive

- The first invalidation of every namespace works
- The invariant is covered by a regression test with an explanatory name

### Negative / accepted cost

- Cache keys read `v0:` before the first invalidation, which looks odd until you know why

## Revisit when

Never. If the version counter is ever reimplemented, the test `test_cache_version_starts_at_zero` documents the requirement.
