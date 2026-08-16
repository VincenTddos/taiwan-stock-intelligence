# Architecture Decision Records

One file per decision. Each records **what was decided, why, what it costs, and
the condition under which it should be revisited** — that last part is what
makes an ADR useful years later, when the person reading it is deciding whether
the constraint still applies.

The index with one-line summaries lives in
[`../ARCHITECTURE.md` §18](../ARCHITECTURE.md#18-architecture-decision-index).
This directory holds the reasoning.

## Numbering

| Range | Phase | Character |
|-------|-------|-----------|
| 001–012 | 0 — Architecture | System shape and principles, decided before any code existed |
| 013–021 | 1 — Foundation | Decisions forced by building it. Three (016, 018, and the `public/` fix noted in 013) came from defects the test suite found |

Numbers are never reused. A superseded ADR keeps its number and gains a
`Superseded by` link; the replacement gains a `Supersedes` link.

## Status values

| Status | Meaning |
|--------|---------|
| `Proposed` | Under discussion, not yet binding |
| `Accepted` | In force |
| `Superseded` | Replaced — see the link in the header |
| `Deprecated` | No longer applies, with nothing replacing it |

## When to write one

Write an ADR when a future reader would reasonably ask *"why on earth is it done
this way?"* — especially when the answer is counter-intuitive. ADR-016 (cache
versions start at zero) is the clearest example: the code looks wrong until you
know that Redis `INCR` on a missing key returns 1, which would make the first
invalidation a no-op.

Do not write one for a decision nobody will question. A directory of obvious
ADRs is as unhelpful as no directory at all.

## Template

```markdown
# ADR-NNN: Short imperative title

| | |
|---|---|
| **Status** | Accepted |
| **Date** | YYYY-MM-DD |
| **Phase** | N — Name |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision
What was decided, stated plainly.

## Context
The situation that forced a choice, and the alternatives.

## Rationale
Why this option. Be concrete about the failure mode being avoided.

## Consequences
### Positive
### Negative / accepted cost

## Revisit when
The measurable condition that should reopen this.
```
