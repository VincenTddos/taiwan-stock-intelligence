# ADR-017: Module boundaries are enforced by the linter

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 1 — Foundation |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

`ruff`'s `flake8-tidy-imports` bans importing `app.api` from anywhere except `app/api/**` and `app/main.py`. Violating the layering fails the lint gate, which fails CI.

## Context

The intended dependency direction is `api → services → repositories → db`. Written in a document, this rule survives roughly as long as the next deadline.

## Rationale

Architectural rules that live only in documentation decay, and they decay silently: nobody notices the first violation, and by the tenth the layering is fiction.

The specific thing being protected is the ability to swap the data access layer without touching business logic. Phase 6 needs to introduce a point-in-time loader that filters everything on `announced_at`. That is a tractable change if all SQL lives in `repositories/`, and an archaeology project if services have been building queries inline.

A lint rule costs nothing per commit and makes the violation impossible to merge. It also documents itself: the error message states the rule.

The rule is deliberately narrow — it bans the one direction that matters most rather than attempting to encode the full dependency graph. A rule nobody can satisfy gets disabled.

## Consequences

### Positive

- The layering cannot silently rot
- The failure message explains the rule at the moment it is broken
- Phase 6's point-in-time loader stays a contained change

### Negative / accepted cost

- Requires per-file ignores for `app/api/**` and tests
- Only covers one edge of the dependency graph

## Revisit when

The service layer grows sub-layers that need their own rules. Extend the banned-api list rather than abandoning the approach.
