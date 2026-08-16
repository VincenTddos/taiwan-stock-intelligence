# ADR-010: No order execution

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

The platform does not connect to brokers and does not place orders. It is a research tool.

## Rationale

Execution brings regulatory obligations, custody of credentials that can move money, and a class of bug whose blast radius is the user's capital. None of that advances the product's actual goal, which is understanding what the market is doing and why.

Excluding it also keeps the security surface small enough to reason about on a single-machine deployment.

## Revisit when

Never, within this project's scope.
