# Contributing

## The one thing that matters most

This is a system that produces numbers people may make financial decisions with.
A wrong feature is annoying; a *plausible-looking wrong number* is dangerous,
because it will be believed. Almost every rule below follows from that.

---

## Non-negotiables

These are enforced by code and CI, not by review etiquette. A change that
violates one of them fails the build.

### 1. Never fabricate market data

```
❌ Sample prices in a seed script
❌ A hard-coded AI Score to make a screenshot look good
❌ A placeholder chart with made-up values
❌ A "temporary" random number generator behind an endpoint
```

If test or demo data is genuinely needed, it must come from a provider whose
`DataSource` is `MOCK`, which forces `is_demo=True`, which surfaces as a red
`DEMO DATA` badge in the UI. `ALLOW_MOCK_DATA=true` makes the application refuse
to start when `APP_ENV=production`.

**No data is an acceptable answer.** Return `404 data-not-available` or an empty
list with `meta.is_stale=true`. Never substitute a value.

### 2. Never let future information reach historical logic

Fundamental data carries both `period_end` (when it was about) and
`announced_at` (when it became knowable). Any historical query filters on
`announced_at`:

```python
# correct
facts = [f for f in facts if f.is_known_at(as_of)]

# wrong — leaks a report that was not published yet
facts = [f for f in facts if f.period_end <= as_of]
```

`FinancialFact` requires `announced_at`, so the unsafe record cannot be
constructed in the first place.

### 3. Never produce a derived value without provenance

`FactorScore` and `AIStockScore` require a `Provenance` block
(`model_version`, `dataset_version`, `feature_version`, `calculated_at`,
`data_as_of`). If you add a new derived type, it carries one too.

### 4. Never put the LLM in a numeric path

The LLM may read text, classify sentiment, extract entities and *describe*
results. It may not compute an indicator, a factor, a score, a backtest metric
or a risk number. Those come from deterministic, versioned code.

Corollary: with `ENABLE_LLM=false` the platform's core must still work.
If your change breaks that, the change is wrong.

### 5. Never present a model output as a certainty

Probabilities, not point predictions. Every model-derived response carries
`confidence` and a disclaimer. The words "guaranteed", "will rise",
"recommend buying" do not appear in generated output.

---

## Module boundaries

```
api  →  services  →  repositories  →  db
```

Dependencies point downward only. `ruff` enforces it:

```toml
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"app.api".msg = "Lower layers must not import from app.api"
```

Practical consequences:

- **All SQL lives in `repositories/`.** Services compose behaviour; they do not
  build queries. This is what will let Phase 6 swap in a point-in-time loader
  without touching business logic.
- **`core/` imports nothing from the layers above it.**
- **Analytics code never calls an external API.** It reads tables that the data
  layer already landed and validated. That is what guarantees "the data a
  backtest sees is the data production sees".

---

## Workflow

```bash
git switch -c feat/short-description
make check          # lint + typecheck + tests + migration reversibility
git commit
```

`make check` is exactly what CI runs. Run it before pushing; it is much faster
than a CI round trip.

### Commit messages

Conventional Commits:

```
feat(market): add TWSE daily price provider
fix(auth): reject refresh token replay after rotation
test(contracts): cover announced_at boundary conditions
docs(architecture): record ADR-013
chore(deps): bump sqlalchemy to 2.0.36
```

Scopes follow module names: `auth`, `health`, `market`, `quant`, `news`,
`ml`, `backtest`, `portfolio`, `infra`, `docs`.

### Pull requests

Include:

1. What changed and why
2. Which Phase / roadmap item it belongs to
3. Test evidence (what you ran, what it proved)
4. Any new technical debt, with the condition under which it should be repaid
5. Documentation updated (`docs/` is part of the codebase, not an afterthought)

---

## Testing standards

Write the test that would have caught the bug, not the test that makes the
coverage number go up.

| Layer | Location | Needs |
|-------|----------|-------|
| Unit | `tests/unit/` | nothing — pure functions and validation |
| Integration | `tests/integration/` | live Postgres + Redis |
| Worker | `tests/worker/` | Redis; `-m worker` needs a live worker |

Rules:

- **Skip loudly.** A test that cannot run must `pytest.skip` with a message
  saying what is missing. A green suite that silently skipped the thing you were
  checking is worse than a red one.
- **Assert behaviour, not implementation.** `test_login_unknown_user_gives_identical_error`
  checks a security property; it would survive a rewrite of the auth service.
- **Test the failure path.** Health checks must not raise when a component is
  down — there is a test that monkeypatches the database into failure and
  asserts the task still returns a report.
- **Real payloads as fixtures.** When Phase 2 adds providers, fixtures are actual
  recorded responses (ROC dates, thousands separators, `"--"` for missing),
  not idealised ones.

Coverage floor is 80%, but coverage is a smoke detector, not a goal.

---

## Adding a migration

```bash
make revision m="add stocks and trading_calendar"
# review the generated file — autogenerate is a draft, not an answer
make migrate-check    # up → down → up
```

- Every revision needs a working `downgrade()`
- Destructive changes ship in two steps: stop writing to the column, observe for
  a cycle, then drop it
- Never edit a migration that has run anywhere other than your own machine
- `alembic check` runs in CI to catch model/migration drift

---

## Adding a configuration value

1. Add the field to `Settings` with a type and a default
2. Add it to `.env.example` with a comment explaining *why it exists*
3. If a wrong value would be dangerous, add a check to `_check_consistency`
   and a test in `tests/unit/test_config.py`

A setting that can silently make the system unsafe should not be able to hold an
unsafe value in the environment where it matters.

---

## Adding an API endpoint

Checklist (from `docs/API_SPEC.md` §23):

- [ ] Returns the standard envelope with a populated `meta`
- [ ] Errors are Problem Details
- [ ] List endpoints paginate, with a `page_size` ceiling
- [ ] Anything slower than ~2s returns `202` + `job_id` instead of blocking
- [ ] Model-derived responses carry `confidence` and a disclaimer
- [ ] Probabilities, never point price predictions
- [ ] Has a Pydantic response model (so it lands in OpenAPI)
- [ ] Integration test covers both the success and the failure path
- [ ] RBAC dependency where appropriate; writes produce an audit log entry

---

## Style

**Python** — ruff (line length 100) + mypy strict. Type everything. `Decimal`
for money and prices, never `float`. Timezone-aware datetimes, always.

**TypeScript** — strict mode, `noUncheckedIndexedAccess`. No `any`. API types
are generated from the backend's OpenAPI schema (`make openapi`); do not
hand-write them.

**Comments** explain *why*, not *what*. The code already says what it does.

```python
# Hash anyway so a missing account and a wrong password take comparable
# time; otherwise login latency enumerates users.
hash_password(password)
```

That comment earns its place. `# hash the password` would not.

---

## Documentation

`docs/` is normative. If a change contradicts a document, either the change is
wrong or the document needs updating in the same PR — never leave them
disagreeing.

An architectural decision that a future reader might question belongs in
`docs/ARCHITECTURE.md` §18 as an ADR, with the condition under which it should
be revisited.

---

## Security

- `.env` is never committed; CI runs gitleaks
- No f-string SQL. Bound parameters or the ORM
- New dependencies need a reason; `pip-audit` and `pnpm audit` run in CI
- Report a vulnerability privately to the maintainer, not in a public issue

When Phase 8 adds the Copilot: it gets a **read-only** database role and a
whitelist of typed tools. It never gets arbitrary SQL. Untrusted text (news,
documents) is wrapped in an explicit data block and never treated as
instructions.
