# ADR-030 — Institutional flow excludes warrants, by the exchange's own selector

- **Status**: Accepted
- **Date**: 2026-11-13
- **Phase**: 2 (found by the first live measurement; fixed after Phase 3 and the Phase 4 foundations were built on top of it)

## Decision

`T86` is requested with `selectType=ALLBUT0999` — everything except warrants,
callable bull/bear contracts and convertible bonds — instead of `selectType=ALL`.
The universe is defined by the exchange's selector, not by a rule of our own
about what a warrant's code looks like.

## Context

The provider asked for `ALL`, which reads like the complete and therefore safe
choice. The first live full-market run wrote 109,459 institutional flow rows and
reported complete success: zero quarantined, zero suspect.

The number was ten times the estimate, and the estimate was not wrong. Measured
on the owner's machine against the live endpoint for 2026-08-14:

| | securities | response |
|---|---|---|
| `selectType=ALL` | 15,637 | 2.0 MB |
| `selectType=ALLBUT0999` | 1,330 | 194 KB |

Of the 15,637, 14,307 had no row in `daily_prices` and no row in `stock_master`.
All 14,307 are six-character codes beginning `03`–`08`: warrants. The 1,330 the
exchange's selector keeps are exactly the securities that had a daily price that
day — 1,082 four-character stocks, 113 five-character codes, 135 six-character
ETFs, ETNs and depositary receipts. The two sets partition the response with no
exceptions in either direction.

Nothing flagged this because nothing was wrong with any row. A warrant's flow is
well-formed, internally consistent and genuinely published by the exchange. No
validity rule can reject it and none should. The defect was in what was asked
for, and a pipeline that validates answers cannot see a bad question.

## Rationale

**Why exclude at all.** The platform cannot price these instruments, has no
master record for them and declares no factor that reads them. Keeping them
costs about 100,000 rows per session — roughly 200 million across a 2019–2026
backfill against 19 million without — to store data nothing can join to. On a
single-machine deployment that is the difference between a table and a problem.

**Why at the request, not after it.** Three places could do this: the request,
the parser, or a filter in the ingestion job. Filtering after the fact means
fetching 2 MB to keep 194 KB, every session, forever, and it means the raw
ingestion record describes a payload ten times the size of what was used. Asking
for what is wanted keeps provenance honest: the recorded response *is* the data.

**Why the exchange's selector, not a code pattern.** The observation that
warrants are six characters beginning `03`–`08` is true of one day's data. It is
evidence for this decision and it is recorded in a fixture, but it is not the
mechanism. The exchange knows what a warrant is; a regular expression knows what
warrants looked like on the day it was written, and it fails silently the day a
new prefix is issued.

**Why a measurement was added as well.** `data_ops_report` now counts
institutional flow symbols that have no price on the same date. It is the number
that would have exposed this on the first run, and it should now be zero. It is
measured rather than assumed so that if the selector's meaning ever changes, the
report says so instead of the table quietly growing tenfold again.

## Consequences

- A session is about 9,300 rows instead of 109,000, and the response is a tenth
  the size.
- Warrant, callable bull/bear and convertible bond flow is not collected. If a
  later phase wants it, that is a second dataset with its own table and its own
  selector, not a widening of this one.
- Rows already written under `ALL` are not removed by this change. They are
  identifiable exactly — `symbols_without_price` in the report — and removing
  them is the owner's decision.
- The backfill estimate in PHASE_2_REPORT §5 (one request per trading day) is
  unchanged; what changes is that each request now returns what the estimate
  assumed it would.

## Revisit when

`symbols_without_price` is non-zero after a clean ingestion, which would mean
the selector no longer matches the priced universe. Or when a phase needs
derivative flow, at which point it gets its own dataset rather than this one.
