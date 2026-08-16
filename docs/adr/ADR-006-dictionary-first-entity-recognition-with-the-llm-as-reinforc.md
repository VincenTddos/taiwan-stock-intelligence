# ADR-006: Dictionary-first entity recognition, with the LLM as reinforcement

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-15 |
| **Phase** | 0 — Architecture |
| **Supersedes** | — |
| **Superseded by** | — |

## Decision

News entity extraction matches against an alias dictionary built automatically from the exchange's company master file. The LLM only extracts entities the dictionary cannot know about — products, technologies, people — and proposes new aliases for human approval.

## Rationale

Taiwanese company aliases are a finite, enumerable set: `台積電 / 2330 / TSMC / 台灣積體電路製造股份有限公司` are the same entity, and the exchange publishes all four forms. An exact-match automaton over that dictionary is deterministic, fast, and cannot hallucinate.

An LLM asked to do the same job will occasionally invent a ticker or miss a company, and will not do so consistently between runs. Using it as ground truth would make the entire news→stock linkage unreliable.

## Revisit when

If measured LLM NER reaches F1 > 0.95 on a Taiwanese financial corpus, its weight in the hybrid can rise — but the dictionary stays authoritative for the entities it covers.
