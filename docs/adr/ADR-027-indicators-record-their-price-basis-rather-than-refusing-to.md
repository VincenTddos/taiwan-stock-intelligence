# ADR-027 — Indicators record their price basis rather than refusing to run unadjusted

- **Status**: Accepted
- **Date**: 2026-08-20
- **Phase**: 3 — Quant Engine
- **Relates to**: [ADR-023](ADR-023-adjustment-requires-verified-corporate-action-coverage.md), [ADR-025](ADR-025-coverage-is-evaluated-per-action-type-and-the-required-set-.md)

## Decision

The quant job asks for adjusted prices, and when the corporate action coverage
guard refuses, it computes on raw prices instead — writing `price_basis='RAW'`
on every row it produces and carrying the refusal's reason in the run report.

It does not fabricate an adjustment, and it does not silently substitute raw
prices for adjusted ones. It also does not stop.

## Context

ADR-023 established that a price series may not be adjusted over a range whose
corporate action coverage has not been recorded. ADR-025 then made that check
per action type, and the honest consequence arrived immediately: **no Taiwanese
symbol can be adjusted over history today**, because TWSE publishes no
decomposable source of historical stock dividends. The guard is right, and it is
closed for every historical range.

That leaves a choice for the entire indicator and factor layer:

1. refuse to compute anything until a source that does not exist yet is ingested
2. compute on raw prices and say so
3. compute on raw prices and not say so

The third is out of the question. The first is defensible and was the initial
instinct — an unadjusted moving average does contain false gaps on ex-dates.

## Rationale

Option 1 fails for a reason that has nothing to do with correctness: it makes
the whole of Phase 3 unrunnable, unreviewable and untestable until an external
data problem is solved. Nothing would have been exercised against real prices,
the performance budget would be unmeasured, and the first run of the pipeline
would happen on the day the data finally arrived — which is the worst possible
day for it to be the first run.

Option 2 works because the compromise is *carried with the data* rather than
remembered:

* `technical_indicators.price_basis` is a column with a CHECK constraint, so
  every stored row states its basis and no query can accidentally mix them
* the run report names the refusal, and the CLI prints it in capitals rather
  than logging it
* the API returns `price_basis` in the payload of every indicator response

The distinction that makes this acceptable is between an indicator and a return
series. A 20-day moving average on raw prices is a real average of real
published prices; it is a poor input to a momentum factor across an ex-date, and
it is exactly right for "where is price relative to its recent range". A *return*
computed across an ex-date is simply wrong. So the guard still stands where it
matters — `AdjustmentService` refuses, ADR-023 is unchanged — and the layer
above it is allowed to run with a label.

The rule this encodes: **a compromise that is recorded in the data is a
compromise; one that is recorded only in someone's memory is a defect.**

## Consequences

### Positive
- Phase 3 is exercisable, measurable and testable today, on real prices.
- The day corporate action coverage arrives, the same job produces
  `price_basis='ADJUSTED'` rows with no code change, and the two are
  distinguishable in the database forever.
- A chart drawn from these numbers can be reconciled with a broker's chart,
  which is only true of unadjusted prices.

### Negative / accepted cost
- Factor values computed today are contaminated by ex-dividend gaps for any
  symbol that went ex during the window. The size of that contamination is not
  estimated anywhere, and the factor validity report cannot separate it from
  genuine signal.
- Anything consuming `factor_scores` must check `price_basis` or inherit the
  contamination silently. The column makes that possible; it does not make it
  automatic.

## Revisit when

A source of historical Taiwanese stock dividends is ingested and the coverage
guard opens over history. At that point the raw-price path should be demoted to
an explicit `--raw-prices` flag for diagnosis only, rather than being the
fallback the job takes on its own.
