# ADR-029 — A news source must prove permission before it is read

- **Status**: Accepted
- **Date**: 2026-08-20
- **Phase**: 4 — News Intelligence

## Decision

A news source cannot be fetched until three facts about it are recorded: a
robots policy that is `NOT_APPLICABLE` or `VERIFIED_ALLOWED`, a licence note
saying what may be stored and redisplayed, and the date somebody established
both. The rule is enforced in three places on purpose:

* a CHECK constraint refuses `news_sources.is_active` without all three
* `ProviderRegistry.get_news` refuses an inactive row
* `NewsProvider.assert_permitted` refuses on the class, independently of the row

Phase 4 therefore starts with two TWSE OpenAPI endpoints — open government data,
no crawler, a published licence — and every media candidate in
`DATA_SOURCES.md` §8.2 sits in the registry as an inactive row with
`robots_policy='UNVERIFIED'`.

## Context

Every other source in this platform is an exchange endpoint whose terms are
settled before anyone asks. News is the first place where fetching something is
a decision with a legal and an ethical side, and where the failure mode is
invisible from the inside: a crawler nobody established was welcome, running
daily from the operator's own address, produces data that looks exactly like
data anybody was allowed to have.

`DATA_SOURCES.md` §8.1 already stated the policy — official feeds and exchange
announcements yes, paywall circumvention and ignoring robots.txt no. A policy in
a document is a policy that holds until the first person who needs one more
source and has not read it.

## Rationale

The three checks are not redundant in the way they look.

The **constraint** is the one an `UPDATE` cannot walk past. Everything else in
this area is code that a person can edit in a hurry, and a source switched on
during an incident is exactly when nobody re-reads the terms.

The **registry check** is where the decision was recorded: it reads the row and
refuses a source that was never signed off.

The **provider check** is about a different question. The row says what was
decided; the class says what the code will actually fetch. A provider pointed at
a path nobody examined should fail on its own account rather than inherit
another row's approval — the row approved a source, not every URL under it.

The error message matters as much as the check: *"it did not object" is not
permission*. The failure this prevents is not somebody deciding to ignore a
robots file; it is somebody never thinking to look.

Two consequences of the same principle, recorded here because they are easy to
mistake for arbitrary restrictions:

**There is no column for an article body.** `news` stores a title, a summary and
a link. The storage policy said the full text is used in the pipeline and not
retained, and a `body` column would have made that a matter of discipline.
Where a source is open government data and copyright does not apply, the storage
argument still does — and one rule everywhere beats a special case that has to
be remembered.

**A source's capabilities are recorded per source.** Whether it attributes items
to a company itself, and whether it publishes a time of day, both change what a
downstream claim is allowed to assert. An item with a date and no time cannot
support an intraday study; recording that on the source is what stops something
later assuming it can.

## Consequences

### Positive
- Phase 4 can be built and tested against real, licence-clean data today.
- The candidate list is visible *as unverified rows*, so the work of checking
  each one is visible with it rather than living in someone's notes.
- The strongest source available turns out to be an announcement feed carrying
  the company's own code and an exact publication time — so the first
  stock-linking path is the publisher's attribution rather than an inference.

### Negative / accepted cost
- Media coverage — the bulk of what a news factor would eventually read — is
  unavailable until each outlet is checked individually, and that is a task with
  a person in it.
- Three enforcement points is more code than one. The duplication is deliberate
  and each has a distinct failure it catches; a future reader who removes two of
  them as redundant will be removing two different guarantees.

## Revisit when

A licensed news vendor is contracted, at which point permission arrives as a
contract rather than a robots file, and `RobotsPolicy` gains a `LICENSED` case
whose evidence is the agreement rather than a fetched file.
