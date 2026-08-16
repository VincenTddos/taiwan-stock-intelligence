# ADR-021: The dashboard shows empty states, never placeholder numbers

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 1 — Foundation |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

Panels with no data source yet render `無資料 / 尚未接入資料來源` and a phase label. No component renders a sample price, a sample score, a sample chart or a zero standing in for an unknown value.

## Context

Phase 1 has no market data. The dashboard still needs a layout, and the conventional way to build one is with representative sample values so the design can be evaluated.

## Rationale

Placeholder market data is uniquely dangerous in this product, for a reason that has nothing to do with the code: **it looks exactly like output**. A screenshot of a dashboard showing "AI Score 91" is indistinguishable from a real one. It gets pasted into a message, it gets remembered, and eventually someone treats it as a finding.

The general engineering rule "don't ship fake data" understates the problem here. The risk is not that fake data reaches production — it is that a *picture* of fake data escapes the development process and is believed.

An empty state also communicates something true and useful: this capability is not built yet, and here is which phase builds it. That is better product information than a fake number.

The same reasoning drives the related enforcement: `DataSource.MOCK` forces `is_demo=True`, which propagates to `meta.is_demo` and renders a red `DEMO DATA` badge; and `ALLOW_MOCK_DATA=true` prevents the application from booting in production.

A `0` is explicitly not an acceptable empty state. Zero is a legitimate value for volume, for net institutional flow, for change — displaying it for "unknown" is a lie the user cannot detect.

## Consequences

### Positive

- No screenshot can be mistaken for real output
- Users learn what is and is not implemented
- Consistent with the contract-level and configuration-level guards

### Negative / accepted cost

- The dashboard looks sparse until Phase 2 lands
- Visual design decisions about dense data are deferred

## Revisit when

Resolves naturally as each phase connects a real source. The rule itself — no fabricated market values, and no zero standing in for unknown — is permanent.
