# ADR-008: The supply-chain graph lives in PostgreSQL, not Neo4j

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

Supply-chain nodes and edges are ordinary tables; multi-hop traversal uses recursive CTEs.

## Rationale

The graph is small: roughly 3,000 listed companies plus a couple of hundred theme and segment nodes, with traversal bounded at three hops. A recursive CTE handles that comfortably, and keeps the graph joinable against prices, scores and news in a single query.

A dedicated graph database would add a service to operate and a second source of truth to keep in sync, in exchange for capability this scale does not need.

## Revisit when

Three-hop traversal exceeds ~500 ms, or the analysis genuinely needs graph algorithms (centrality at scale, community detection) that SQL expresses badly.
