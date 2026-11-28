# ADR-024 — The adjustment factor is the exchange's formula, and is reconciled against the exchange's own price

- **Status**: Accepted
- **Date**: 2026-08-19
- **Phase**: 2.5 (the corporate action prerequisite Phase 3 blocks on)

## Decision

The single-day adjustment factor for a TWSE symbol is `reference / close`, where
`reference` is the exchange's published formula:

```
除權息參考價 = (除權息前收盤價 − 現金股利 + 現金增資認購價 × 現金增資配股率)
              / (1 + 無償配股率 + 現金增資配股率)
```

Each action row on that ex-date stores the part of the factor attributable to it,
split so that the product over the day's rows is the day factor exactly:

| component | factor |
|-----------|--------|
| cash | `(P − D) / P` |
| rights | `(P − D + kS) / ((P − D)(1 + k))` |
| stock / split | `(1 + k) / (1 + s + k)` |

Where the exchange also published `除權息前收盤價` and `除權息參考價`, their ratio
is computed independently and the difference recorded in basis points. It is
**not** used as the factor — except for capital reductions, which are the single
documented exception.

A day the code cannot resolve is reported as unresolved and blocks its symbol
entirely. There is no partial adjustment.

## Context

An adjustment is one of the very few numbers in this system with a published
right answer sitting next to it. TWSE prints its own reference price for every
ex-rights and ex-dividend event, which makes two things possible that are usually
not: the formula can be copied rather than chosen, and the result can be checked
rather than asserted.

Three things forced the details.

**The components are not independent.** An ex-date may carry a cash dividend, a
stock dividend and a rights issue at once, and each arrives as its own row from
its own parser. The obvious approach — compute each row's textbook factor and
multiply — is wrong, because TWSE puts the stock ratio and the subscription ratio
in *one* denominator. The naive product is off by the cross term `s·k`. Small, and
permanent, and in the direction nobody would notice.

**The exchange's reference price is an answer, not the answer.** It is rounded to
two decimals, so on a NT$9.76 stock it is up to ~9 bps away from the exact
arithmetic — measured, not assumed: across the recorded 2026 payload the residuals
run 0.05 to 9.29 bps. Using it directly as the factor would bake TWSE's display
rounding into a decade of returns. Using it as a check catches a wrong dividend
immediately.

**Capital reductions cannot be decomposed at all.** A Taiwanese cash reduction
both returns money and cancels shares, and TWSE publishes one observable — the
price change. One equation, two unknowns. Back-solving either would put a number
with no source into the adjustment chain.

## Rationale

Matching the exchange's convention is not deference; it is the only way a chart
here and a number on twse.com.tw can be compared without first establishing which
convention each used. Any other defensible formula would put this platform in
permanent small disagreement with every published Taiwanese figure, which is
worse than being wrong in a visible way.

Storing the per-row share is what makes an adjusted price *explainable*: the chain
from a published dividend to a changed close is one join, not a rerun of the
service. That is only worth doing if the shares multiply out exactly, so they are
derived to, and a test asserts it.

The capital reduction exception is narrow and stated: the published price effect
is used because it is the only sourced number available, not because published
prices are generally preferred. Every other class uses the economics and treats
the published price as an independent check.

Refusing to produce a partial adjustment is the same argument as ADR-023 one
layer down. A series adjusted for the dividend but not the rights issue three days
later looks exactly like a correct one and is wrong by the size of the event
nobody could price — so an unresolvable event blocks its whole symbol rather than
being skipped.

## Consequences

### Positive
- Every factor is checkable against an independent published number, and the
  residual is recorded rather than discarded.
- A parse error large enough to matter fails reconciliation instead of quietly
  rewriting history; a test proves the check can fail.
- The stored per-row factors make "why did this close change" answerable.

### Negative / accepted cost
- The reconciliation tolerance (30 bps) is a judgement. It sits above TWSE's
  display rounding on cheap stocks and far below any real error, but it is a
  threshold and thresholds age.
- The decomposition assigns the shared dilution denominator to the stock row and
  the subscription term to the rights row. The split is exact but not unique;
  another attribution would multiply to the same day factor.
- A symbol with one unpriced event is unadjustable in full, even for years the
  data is complete. Correct, and occasionally inconvenient.

## Revisit when

TWSE changes its published reference price formula, or a second market is added
whose convention differs — at which point the formula becomes per-market rather
than a constant, and this ADR gains a sibling rather than an amendment.
