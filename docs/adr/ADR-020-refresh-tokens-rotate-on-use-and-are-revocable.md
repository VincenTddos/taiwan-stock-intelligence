# ADR-020: Refresh tokens rotate on use and are revocable

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 1 — Foundation |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

Every successful refresh issues a **new** refresh token and adds the presented token's `jti` to a Redis denylist with a TTL matching its remaining lifetime. Logout adds the token to the same denylist.

## Context

Access tokens are stateless JWTs with a 15-minute lifetime. Refresh tokens live 7 days. A stolen refresh token without rotation grants a week of access with no way to stop it.

## Rationale

Rotation gives two properties that a static refresh token cannot:

**A stolen token is usable at most once.** After the attacker uses it, the legitimate client's next refresh fails — and vice versa. Either way the theft produces a visible symptom instead of silent long-term access.

**Logout becomes real.** Without a denylist, "logging out" only clears client-side storage; the token remains valid until expiry. With it, revocation is immediate.

Redis is the right store: entries are keyed by `jti`, expire on their own when the token would have expired anyway, and the denylist is bounded by the number of refreshes in a 7-day window. There is no cleanup job and no unbounded growth.

The cost is that refresh is no longer stateless — it requires a Redis round trip. On a single-machine deployment where Redis is already on the critical path for caching, this is not a meaningful constraint.

## Consequences

### Positive

- Token theft becomes detectable rather than silent
- Logout genuinely revokes
- The denylist self-expires; no maintenance
- Covered by `test_refresh_rotates_and_revokes_old_token`

### Negative / accepted cost

- Refresh depends on Redis being available
- A client that races two refreshes will have one fail (correct, but needs handling)

## Revisit when

Moving to httpOnly cookies with a server-side session would supersede this. Until then, rotation is the strongest option available to a Bearer-token client.
