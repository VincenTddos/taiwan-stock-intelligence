# ADR-007: Bitemporal fundamental data

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

Every fundamental record stores both `period_end` (when the fact was about) and `announced_at` (when it became public). Historical queries filter exclusively on `announced_at`.

## Rationale

Taiwanese quarterly reports are disclosed up to 45 days after period end; annual reports up to 75 days. A backtest that aligns on `period_end` is reading reports that had not been published, and the resulting performance is fiction.

This is the single most common way a backtest silently becomes wrong, and no amount of care at the query site prevents it reliably. Storing both times and requiring `announced_at` at the type level does.

## Revisit when

Never. This is a principle, not a trade-off.
