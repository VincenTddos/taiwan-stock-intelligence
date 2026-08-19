# ADR-026 — The indicator library reproduces TA-Lib's seeding exactly

- **Status**: Accepted
- **Date**: 2026-08-20
- **Phase**: 3 — Quant Engine

## Decision

Indicators are implemented in polars and are **not** a dependency on TA-Lib at
runtime. TA-Lib is a development dependency and the golden reference: every
indicator it defines is asserted equal to its output at `rtol=1e-6`, including
the exact index at which each indicator begins.

Reproducing that tolerance requires reproducing TA-Lib's *seeding*, which is
idiosyncratic and is now implemented deliberately:

| average | seed |
|---------|------|
| EMA / Wilder | simple mean of the first `window` observations, placed at `window - 1` |
| DI / DX accumulator | plain sum of the first `window - 1` observations, first output at `window - 1` |
| MACD's fast EMA | seeded at the *slow* EMA's start, from the `fast` bars ending there |
| ADX | Wilder average of DX, itself starting at `window` |

## Context

`QUANT_ENGINE.md` §1.1 already ruled out TA-Lib as a runtime dependency: it is a
C library, and making a cross-platform deployment depend on compiling it is a
cost paid forever for arithmetic that is a few dozen lines of polars. It also
named TA-Lib as the test-time golden reference, which is the part that turned
out to have teeth.

The naive implementation of every recursive average — start the recursion at the
first observation, which is what `ewm_mean` does — agrees with TA-Lib to about
1%, converging as the series lengthens. On a chart the two are
indistinguishable. At `rtol=1e-6` they never agree, anywhere, ever.

Three specific discoveries came out of forcing that tolerance:

* **+DI and −DI seed on `window - 1` observations, not `window`.** The
  directional indicators use a running *sum* rather than an average, and TA-Lib
  accumulates one fewer observation before starting. Since DI is a ratio of two
  such sums, the difference does not cancel: the first output was out by 0.3.
* **MACD's fast EMA is seeded where MACD needs it, not where a 12-period EMA
  would start.** TA-Lib begins each internal average at the index its caller
  requires, so the fast leg is seeded from the twelve bars ending at bar 25.
  Seeded the "natural" way, the MACD line is out by ~0.2 for a hundred bars.
* **A MACD line published before its signal would depend on the signal period.**
  Because of the above, the same series would take different values depending on
  a parameter that is supposed to affect only the signal. It is therefore
  published only from where the signal exists — which is also what TA-Lib does.

## Rationale

The tolerance is the decision. `rtol=1e-3` would have passed on the first
attempt, and the library would have shipped with four indicators that are
approximately right — the kind of error that never announces itself and shifts
every backtest by a little.

Tightening to `1e-6` converts "looks the same" into "is the same", and each
failure it produced was a real difference in definition rather than in floating
point. The cost is that the seeding logic is more complicated than the formulas,
which is why it lives in one module (`app/quant/core.py`) with the reasoning
attached rather than being repeated per indicator.

Three indicators deliberately do **not** match TA-Lib, and each has a test that
says why rather than a widened tolerance:

* `kd_k` / `kd_d` follow the Taiwanese convention (RSV smoothed by thirds), not
  TA-Lib's `STOCH`. They are different indicators sharing a name, and every
  Taiwanese platform shows the former.
* `roc` returns a fraction where TA-Lib returns a percentage, because every
  consumer downstream works in fractions.
* `beta`, `alpha`, `corr` and `idio_vol` have no TA-Lib equivalent and are
  checked against an independent numpy least-squares fit.

## Consequences

### Positive
- The deployed image needs no C library, and the numbers still equal the
  reference the rest of the industry quotes.
- Forty-two golden assertions fail loudly if anyone "simplifies" the seeding.
- A test demonstrates that the naive implementation would pass at `1e-2` and
  fail at `1e-6`, so the tolerance cannot be relaxed by someone who does not know
  what it is buying.

### Negative / accepted cost
- `core.py` contains machinery that exists purely to imitate another library's
  initialisation. It is the least intuitive code in the package.
- TA-Lib must be installable in CI. Where it is not, the golden tests skip with
  a message saying that is a gap in the environment, not a pass.

## Revisit when

TA-Lib changes its seeding (it has been stable for two decades), or an indicator
is added that TA-Lib does not define — in which case its reference must be
stated explicitly, as `beta` and `kd` already are.
