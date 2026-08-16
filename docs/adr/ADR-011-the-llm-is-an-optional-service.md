# ADR-011: The LLM is an optional service

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

Ollama runs under an opt-in compose profile, gated by `ENABLE_LLM`. With it off, market data, quant, database, API and backtesting all work unchanged; news processing falls back to the rule-based path and the Copilot reports itself unavailable.

## Rationale

A 14B model needs roughly 10 GB of VRAM or a lot of RAM. On a personal machine also running Postgres, Redis, Next.js and Celery workers, that may simply not be available — and the platform must not become unusable because of it.

Making this structural rather than aspirational is what ADR-005 buys: since no number depends on the LLM, switching it off degrades one feature instead of the system.

## Revisit when

Never. The guarantee is asserted by `test_llm_disabled_does_not_degrade_the_system`.
