# ADR-019: `disabled` is a first-class health status, distinct from `degraded`

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 1 — Foundation |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

A component that is intentionally switched off reports `disabled`. Aggregation ignores `disabled` components entirely, so an optional component being off never drags the system status below `healthy`.

## Context

ADR-011 requires the platform's core — market data, quant, database, API, backtesting — to work with the LLM switched off. With `ENABLE_LLM=false` the LLM component cannot be `healthy` (nothing is running) and reporting it `unhealthy` or `degraded` would be misleading.

## Rationale

If a deliberately-disabled component degrades the system status, the health page sits permanently amber. A permanently amber dashboard is one nobody looks at, which means the *next* real degradation goes unnoticed. The value of a status indicator is entirely in the contrast between its normal and abnormal states.

`disabled` says something different from both `healthy` and `unhealthy`: *this is off because you turned it off*. That is actionable information — it tells an operator debugging a missing Copilot response exactly where to look — without being an alarm.

The distinction also keeps the LLM out of `REQUIRED_COMPONENTS`, which is the mechanism that makes ADR-011's guarantee testable: `test_llm_disabled_does_not_degrade_the_system` asserts the system is not degraded while the LLM is off.

## Consequences

### Positive

- The health page stays green when the system is genuinely fine
- Operators can distinguish "off" from "broken" at a glance
- ADR-011's guarantee becomes machine-checkable

### Negative / accepted cost

- One more status for consumers of the health API to handle

## Revisit when

Never. This is a display-semantics decision that has aged well in every system that makes it.
