# ADR-028 — The feature version is a hash of the specification

- **Status**: Accepted
- **Date**: 2026-08-20
- **Phase**: 3 — Quant Engine

## Decision

`feature_version` is `quant.v1-<first ten hex of sha256(spec document)>`, where
the spec document is every indicator's name, inputs and parameters, every
`FactorSpec` as data, and the validity thresholds. It is computed, never chosen.
It is part of the primary key of `technical_indicators`, `factor_scores` and
`factor_validity`, and the row registering it stores the full document alongside
the git sha of the code that ran.

## Context

Every stored factor score is meaningless without the definition that produced
it. `momentum_12_1` winsorised at 1%/99% and neutralised against sector and size
is a different number from the same name winsorised at 5%/95% — and the two are
indistinguishable in a table that records only the name.

The conventional answer is a version string someone increments. The failure mode
of that answer is specific and reliable: it is incremented for the changes
someone noticed, and not for the one that mattered. A threshold nudged during
debugging, a window changed in a refactor, a direction flipped after a review —
each produces different numbers under an unchanged version, and every score
written before and after becomes silently incomparable.

## Rationale

Content addressing removes the human step. The version cannot fail to change
when the definition changes, because it *is* the definition. Two deployments
that computed the same definitions get the same version, so a redeploy does not
fork the history; a single altered parameter gets a different one, so the old
scores keep their meaning.

The version is in the primary key rather than beside it so that recomputation
adds rather than overwrites. A model trained last quarter can still be shown the
exact numbers it was trained on — which is what makes its reported metrics
checkable rather than merely recorded.

`code_version` is stored next to `spec` because the spec cannot hold the code.
The document says what was intended; the git sha says what actually ran. Either
alone is half a record, and the half that is missing is always the one being
argued about.

## Consequences

### Positive
- A change to any parameter that affects a number produces a new version
  automatically, with no discipline required.
- `feature_versions.spec` is complete enough to reconstruct the definitions
  without reading the code, which is what `dataset_version` / `feature_version` /
  `model_version` in the Phase 1 contract were designed for.
- A test asserts that changing a threshold changes the version, so the mechanism
  cannot quietly stop working.

### Negative / accepted cost
- The version is not human-readable and carries no ordering. `quant.v1-716b6be556`
  says nothing about whether it is newer than `quant.v1-0a4f2c91bb`; that comes
  from `created_at`.
- Cosmetic changes — a reworded description, a reordered registry — produce a new
  version and therefore a full recomputation, even though no number changed. The
  alternative is deciding which fields are cosmetic, and that decision is exactly
  the judgement this ADR exists to remove.

## Revisit when

Recomputation becomes expensive enough that a cosmetic change forcing one is a
real cost — at which point the fix is to exclude descriptions from the hashed
document, not to hand the version back to a person.
