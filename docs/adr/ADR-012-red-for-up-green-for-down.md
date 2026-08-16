# ADR-012: Red for up, green for down

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

Price movement uses red for gains and green for losses, opposite to US convention.

## Rationale

This is the universal convention in Taiwanese markets. A user scanning a heatmap does not read the legend; they read the colour. Inverting it would make every chart actively misleading to the intended audience — worse than having no colour at all.

## Revisit when

A setting may be offered, but the default does not change.
