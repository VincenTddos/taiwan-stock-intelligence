# ADR-009: LightGBM as the first-stage model

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

The first ML models are gradient-boosted trees (LightGBM), not sequence models.

## Rationale

On financial tabular data with a low signal-to-noise ratio, gradient boosting is the reliable baseline: it trains in seconds on CPU, tolerates missing values, and supports SHAP so that every prediction decomposes into feature contributions.

Starting with an LSTM or a transformer means spending the project's early credibility on a model whose failures are hard to diagnose and whose advantage over boosting on this data is unproven.

## Revisit when

A boosting baseline exists, is properly walk-forward validated, and its errors show structure that a sequence model would plausibly capture.
