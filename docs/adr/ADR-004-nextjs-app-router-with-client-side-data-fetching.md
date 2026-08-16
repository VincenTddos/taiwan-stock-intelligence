# ADR-004: Next.js App Router with client-side data fetching

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

The frontend uses Next.js App Router, but fetches data client-side via TanStack Query rather than in server components.

## Rationale

This is a personal research tool: there is no SEO requirement and no cold-start audience. Client-side fetching keeps the data flow in one place and avoids reasoning about which code runs where.

App Router is still the right foundation because moving individual routes to server components later is incremental.

## Revisit when

The platform is exposed to other users and first-paint latency becomes a real concern. Then migrate the heaviest routes to RSC with streaming.
