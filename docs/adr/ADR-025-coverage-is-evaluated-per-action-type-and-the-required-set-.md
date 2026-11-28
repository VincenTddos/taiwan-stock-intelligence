# ADR-025 — Coverage is evaluated per action type, and the required set is per market

- **Status**: Accepted
- **Date**: 2026-08-19
- **Phase**: 2.5
- **Refines**: [ADR-023](ADR-023-adjustment-requires-verified-corporate-action-coverage.md)

## Decision

Two changes to the coverage guard ADR-023 established:

1. A symbol is adjustable over a range only if, **for every required action
   type**, the sources that can see that type together span the range. The union
   of ranges and the union of types are no longer taken separately.
2. The required set is looked up per market. For TWSE it is cash dividends, stock
   dividends, rights issues and capital reductions — `SPLIT` and `REVERSE_SPLIT`
   stay in `PRICE_AFFECTING` but are not demanded.

## Context

Both changes came from the same afternoon: wiring the three TWSE corporate action
endpoints into an actual ingestion job, and finding out what the guard then said.

**The union bug.** `gaps()` computed `min(covered_from)`, `max(covered_to)` and
the union of `action_types` across a symbol's coverage rows, then compared each
separately. With TWSE's real sources that reads as follows: the results table
covers cash dividends from 2019 to today, the forecast table covers stock
dividends and rights issues from *today* forward — so the union is "2019 to today,
every type", and the guard opens. Neither source has ever seen a 2019 stock
dividend. This is exactly the false confidence the coverage table was built to
make impossible, reintroduced by the arithmetic used to read it.

**The impossible requirement.** `PRICE_AFFECTING` includes `SPLIT` and
`REVERSE_SPLIT`. No TWSE source reports either, and none ever will: a Taiwanese
split is executed as a 無償配股 (which the ex-rights tables do report) or a
面額變更, and the reverse direction as a 減資 (reported separately). Requiring
split coverage of a TWSE symbol is therefore a condition nothing can satisfy, and
the guard would refuse every symbol forever.

## Rationale

Per-type evaluation is what the table's own schema always implied — `action_types`
is stored per row precisely because sources differ in what they can see — and the
reading code simply had not honoured it. The error message improves as a side
effect: it now names which types are short and over what window, instead of
reporting a range that no single type was actually covered over.

The market-scoped required set is the less obvious call, because narrowing a
safety check looks like weakening it. The alternative is worse in a specific way:
a guard that can never open does not protect anything, it just moves the decision
to whoever adds `--force` to the command line, at which point the refusal carries
no information at all. A guard is only a guard while passing it means something.
The types are kept in `PRICE_AFFECTING` and in the schema because a split *would*
move a price if one occurred, and a market that publishes them will require them.

## Consequences

### Positive
- The guard now states a true condition. On live data it refuses a 2019–2026
  request for 2330 and names `RIGHTS_ISSUE` and `STOCK_DIVIDEND` as covered only
  from 2026-08-19 — which is the actual state of TWSE's published sources.
- Adding a source with real stock-dividend history opens the guard for exactly
  the range that source covers, with no further change.

### Negative / accepted cost
- Coverage is still a union of ranges per type, not an interval algebra: a hole
  between two disjoint runs for one type is not detected. Ingestion advances a
  contiguous window, so such a hole has to be created deliberately. Recorded here
  because it is a real limit, not because it is a likely one.
- The required set is now a table someone must maintain when a market is added.
  Getting it wrong in the permissive direction is silent, which is the same class
  of failure this whole area exists to avoid — so it carries the reasoning inline.

## Revisit when

A source of historical Taiwanese stock dividends exists (MOPS `t187ap45_L` per
year, or a vendor), at which point the forecast table stops being the only
decomposed source and the guard opens over history. Also revisit if any market is
added, which requires an entry in `REQUIRED_FOR_ADJUSTMENT` and a written reason.
