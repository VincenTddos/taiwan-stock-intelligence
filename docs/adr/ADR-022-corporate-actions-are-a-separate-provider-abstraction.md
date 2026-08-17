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

## What the payloads actually showed

The brief named `t187ap45_L` as the first provider. Fetching it changed that.

`t187ap45_L` (上市公司股利分派情形) **has no ex-date**. Its three date fields are
`出表日期`, `董事會（擬議）股利分派日` and `股東會日期` — when the decision was
made, never when the adjustment takes effect. It cannot drive an adjustment
alone.

Four official endpoints together can, and the split falls exactly along the
bitemporal axis the platform already models:

| Endpoint | Axis | Carries |
|----------|------|---------|
| `TWT48U_ALL` 除權除息預告表 | event time, ahead of the fact | ex-date, cash, stock ratio, subscription ratio and price |
| `TWT49U` 除權除息計算結果表 | event time, after the fact | ex-date, pre-close, reference price, 權值+息值 |
| `t187ap45_L` 股利分派情形 | **knowledge time** | board resolution and shareholder meeting dates, itemised composition |
| `TWTAUU` 減資恢復買賣參考價格 | event time | capital reductions, pre/post price, 退還股款 vs 彌補虧損 |

So `supported_actions` and the union-across-sources coverage rule are not
hypothetical generality — they are required by the first market this platform
targets, from its own exchange, on day one.

Measured while confirming this: `TWT49U` serves a full year in one request (880
rows for 2019, identical fields to 2026), and `TWTAUU` serves 2019–2026 in one
request (174 rows). Corporate action backfill is therefore single-digit requests,
not the per-day iteration daily prices need.

One gap is recorded rather than papered over: `TWTAUU` publishes prices but not
the cash returned per share. For a 退還股款 reduction the price change reflects
both the cash and the share ratio, and one equation cannot separate two unknowns.
`cash_returned_per_share` stays null until a source that publishes it is added.

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
