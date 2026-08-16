# ADR-001: Single PostgreSQL for relational, time-series and vector data

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

One PostgreSQL 16 instance carries all three workloads, using the TimescaleDB and pgvector extensions rather than separate datastores.

## Rationale

The product's characteristic query joins across all three: *find stocks in the AI supply chain whose AI Score rose fastest and that have positive news*. In one database that is a single SQL statement. Split across three, it becomes three round trips and a manual join in application code.

On a single machine, running one datastore instead of three also removes two services to operate, back up and monitor.

## Revisit when

Vectors exceed ~5 million rows **and** retrieval exceeds 200 ms, or time-series exceeds ~500 million rows. Measure before acting — see ARCHITECTURE.md §16.
