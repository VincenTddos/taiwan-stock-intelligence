# ADR-022 — Corporate actions are a separate provider abstraction

- **Status**: Accepted
- **Date**: 2026-08-17
- **Phase**: 3 (design; first implementation deferred)

## Decision

Corporate actions are served through `CorporateActionProvider`, an abstraction
separate from `BaseMarketDataProvider`, resolved from the `data_sources` registry
via `ProviderRegistry.get_corporate_actions`. TWSE `t187ap45_L` will be the first
implementation. No exchange name or endpoint appears in the domain or service
layer.

## Context

The Phase 3 brief selects TWSE `t187ap45_L` (除權除息計算結果表) as the first
corporate action source. The obvious implementation is to add a
`get_corporate_actions` method to the existing price provider, since TWSE serves
both.

That would tie two decisions together permanently. The source that publishes a
closing price is not necessarily the source that publishes a dividend: MOPS
carries board resolutions earlier and in more detail than the exchange's
ex-rights table, a licensed vendor would supply both plus the events neither
covers, and TPEx securities need a different price source but may share a
corporate action source. Making "who gives us prices" and "who gives us
corporate actions" the same choice would mean changing one to change the other.

## Rationale

**Two boundaries are drawn deliberately.**

*Providers report economic facts, not adjustment factors.* A provider says
"NT$2.50 cash and 0.05 shares per share, ex on this date". It does not say what
that does to a price series. The adjustment formula is a domain decision, it is
market-specific, and more than one convention is defensible — so it lives in a
service where it can be tested and changed once, rather than being reimplemented
by every provider.

*A provider declares which action types it can see* (`supported_actions`).
Without that, "no rights issue found" and "this source has never carried rights
issues" are the same answer, and a return series silently loses a real
adjustment. Declaring capability turns the second case into a coverage fact that
`CorporateActionCoverageService` can act on.

`CORPORATE_ACTION_PROVIDER_TYPES` is empty until Phase 3 registers an
implementation, and a lookup against it raises `NOT_SUPPORTED` rather than
falling back to a price provider that would answer every question with nothing.

## Consequences

- One more abstraction to maintain, and a second registry lookup path.
- A source can be added for corporate actions alone, without touching prices.
- Providers stay thin; the adjustment formula has exactly one home.
- Until an implementation is registered, any attempt to fetch corporate actions
  fails loudly. That is intended: silence here is what produces contaminated
  return series.

## Revisit when

A second corporate action source is added and the two disagree. Reconciliation
across sources is not designed yet; the schema keeps `source` in the natural key
so disagreement is representable rather than overwritten.
