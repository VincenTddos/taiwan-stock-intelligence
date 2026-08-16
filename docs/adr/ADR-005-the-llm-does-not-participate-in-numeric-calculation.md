# ADR-005: The LLM does not participate in numeric calculation

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

Indicators, factors, scores, backtest metrics, risk numbers and portfolio analytics are computed by deterministic, versioned code. The LLM reads text and describes results; it never produces a number that is stored or displayed as a measurement.

## Rationale

Two properties are non-negotiable for this product: **reproducibility** (the same inputs must give the same score) and **explainability** (a score must decompose into contributions that sum to it). An LLM provides neither. Its output varies between runs and cannot be versioned as a computation.

This also keeps the LLM optional — see ADR-011. If the LLM were in the scoring path, disabling it would disable the product.

## Revisit when

Never. This is a principle, not a trade-off.
