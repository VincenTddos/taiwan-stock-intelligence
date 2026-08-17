# ADR-023 — Adjusted prices require verified corporate action coverage

- **Status**: Accepted
- **Date**: 2026-08-17
- **Phase**: 3 (precondition; adjustment arithmetic deferred)

## Decision

No adjusted price, return, moving average or momentum value may be computed over
a symbol and date range whose corporate action coverage has not been recorded.
`CorporateActionCoverageService.assert_adjustable` raises `AdjustmentNotPermitted`
otherwise. Coverage is stored as a positive fact in `corporate_action_coverage`.

## Context

On an ex-dividend or ex-rights date the raw close drops by an amount that has
nothing to do with anyone's view of the company. A series that has not been
adjusted records that as a return.

The reason this needs a structural guard rather than care is the shape of the
failure. The contaminated numbers do not look wrong. A 3% ex-dividend gap is an
ordinary day's move: it trips no anomaly rule, looks unremarkable on a chart, and
enters a momentum factor as signal. Worse, it is not noise — Taiwanese ex-dates
cluster heavily in Q3, so the contamination has calendar structure, and a
backtest will find that structure and report it as a discovery.

The naive alternative is to adjust using whatever rows happen to be in
`corporate_actions`. That treats an empty result as "no actions occurred", when
it may equally mean "nobody has fetched actions for this symbol". Both produce an
empty set, and only one of them makes adjustment safe.

## Rationale

Coverage is recorded rather than inferred. `corporate_action_coverage` stores,
per symbol and source, the date range searched and the action types the source
was capable of reporting — so:

- a symbol with genuinely zero actions is adjustable once someone has looked
- a symbol nobody has looked at is not
- full date coverage from a dividends-only source still leaves a gap for capital
  reductions, and says so specifically

Coverage from multiple sources unions, because one source may cover dividends
and another structural events, and together they can be complete. Ranges widen
rather than replace, so a backfill advancing through history accumulates rather
than appearing to destroy its own progress.

`PAR_VALUE_CHANGE` is excluded from the required set: it alters stated par value
without changing what a holder owns, so it does not enter the factor. It stays in
the schema because it belongs in the record of what happened.

The failure is an exception, not a warning. A warning would be logged, ignored,
and the contaminated series used anyway.

## Consequences

- Phase 3 cannot produce a single factor value until corporate actions are
  actually ingested. This is the point, and it makes the dependency visible at
  the start rather than after the results look wrong.
- Every adjustment entry point must call `assert_adjustable` first. A code path
  that skips it is reading ex-dividend gaps as returns; a test asserting that
  each entry point is gated should accompany the first implementation.
- Backfill must write coverage as it advances, or ranges it has already covered
  will keep being refused.

## Revisit when

Coverage becomes expensive to check per query — at which point it caches, but the
rule does not relax. Also revisit if a source is added whose capability varies by
date rather than being constant; `action_types` is currently one set per source.
