"""Shared numerics for the indicator library.

Two of these matter more than they look.

`wilder` and `seeded_ema` exist because TA-Lib is the golden reference and
TA-Lib seeds its recursive averages with a simple mean rather than starting the
recursion at the first observation. `Series.ewm_mean` starts at the first
observation, so a naive implementation agrees with TA-Lib only asymptotically —
close enough to look right on a chart, and never equal at 1e-6. Seeding is
reproduced exactly here, vectorised, rather than looped per row: at 2,000
symbols by 2,500 days a Python-level recursion is the whole performance budget.

`safe_div` exists because a zero denominator is a real market condition — a
suspended stock has no range and no turnover — and NaN is the correct answer,
not an exception and not zero.
"""

from __future__ import annotations

import polars as pl

TRADING_DAYS_PER_YEAR = 252


def as_float(value: object) -> float | None:
    """Coerce a polars aggregate to a float.

    `Series.mean()` is typed as any of a dozen things because a Series may hold
    any of a dozen things. Every caller here has already cast to Float64, so the
    narrowing is real rather than a silenced complaint — and a non-numeric
    result becomes None instead of an exception three frames away.
    """
    if isinstance(value, int | float):
        return float(value)
    return None


def align(values: pl.Series, length: int, name: str | None = None) -> pl.Series:
    """Left-pad with nulls to exactly `length`, truncating if longer.

    Every indicator must return one value per input bar, null where undefined.
    Several of them build their output as `nulls + body`, and when a symbol has
    fewer bars than the indicator's warm-up the body is empty while the padding
    is not — so the result comes back longer than the input. That is not a
    cosmetic mismatch: a newly listed company is exactly this case, and polars
    raises on the shape rather than producing a wrong number, which is the good
    outcome. Going through here makes it impossible to reach.
    """
    if values.len() > length:
        values = values.slice(values.len() - length, length)
    if values.len() < length:
        pad = pl.Series([None] * (length - values.len()), dtype=pl.Float64)
        values = pl.concat([pad, values.cast(pl.Float64)])
    return values.rename(name or values.name or "")


def safe_div(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    """Divide, yielding null where the denominator is zero or null."""
    return (
        pl.when(denominator.is_null() | (denominator == 0))
        .then(None)
        .otherwise(numerator / denominator)
    )


def seeded_ema(values: pl.Series, window: int, alpha: float, start: int | None = None) -> pl.Series:
    """An exponential average seeded with a simple mean of `window` observations.

    Output before `start` is null; the value at `start` is the simple mean of
    the `window` observations ending there; everything after is
    `alpha * x + (1 - alpha) * previous`. That is precisely what TA-Lib does,
    and reproducing it is the difference between agreeing with the reference and
    merely converging towards it.

    `start` defaults to `window - 1`, which is where the average can first be
    formed. It is a parameter because TA-Lib starts an EMA at the index the
    *caller* needs rather than the earliest possible one — inside MACD the fast
    EMA is seeded from the twelve bars preceding the slow EMA's own start, not
    from the first twelve bars of the series. Same function, different seed,
    different values for a long while afterwards.
    """
    n = values.len()
    if window <= 0:
        raise ValueError("window must be positive")
    begin = window - 1 if start is None else start
    if begin < window - 1:
        raise ValueError("start cannot precede the first full window")
    if n <= begin:
        return pl.Series(values.name, [None] * n, dtype=pl.Float64)

    seed = as_float(values.slice(begin - window + 1, window).mean())
    if seed is None:
        return pl.Series(values.name, [None] * n, dtype=pl.Float64)

    # The recursion starts at `begin`, holding the seed. Feeding the seed in as
    # the first element and letting the recursion continue over the untouched
    # tail gives the same sequence without a per-row loop.
    tail = values.slice(begin + 1, n - begin - 1).cast(pl.Float64)
    body = pl.concat([pl.Series([seed], dtype=pl.Float64), tail])
    smoothed = body.ewm_mean(alpha=alpha, adjust=False)

    head = pl.Series([None] * begin, dtype=pl.Float64)
    return pl.concat([head, smoothed]).rename(values.name or "")


def wilder(values: pl.Series, window: int, start: int | None = None) -> pl.Series:
    """Wilder's smoothing: an EMA with `alpha = 1 / window`, seeded by mean."""
    return seeded_ema(values, window, alpha=1.0 / window, start=start)


def talib_ema(values: pl.Series, window: int, start: int | None = None) -> pl.Series:
    """TA-Lib's EMA: `alpha = 2 / (window + 1)`, seeded by mean."""
    return seeded_ema(values, window, alpha=2.0 / (window + 1), start=start)


def wilder_sum(values: pl.Series, window: int) -> pl.Series:
    """Wilder's running sum as the directional indicators use it.

    `sum_t = sum_{t-1} - sum_{t-1} / window + x_t`, seeded with the plain sum of
    the first **window - 1** observations — one fewer than the smoothed average
    uses. That asymmetry is not a quirk of this implementation; it is what
    TA-Lib does, and +DI is the ratio of two of these sums, so the difference
    does not cancel. Seeding it the intuitive way puts the first output out by
    about 0.3 and every later one by less, which converges and never agrees.

    The first `window - 1` outputs are null and the value at `window - 1` is the
    first real one.
    """
    n = values.len()
    if window <= 1:
        raise ValueError("window must be greater than 1")
    if n < window:
        return pl.Series(values.name, [None] * n, dtype=pl.Float64)

    seed = as_float(values.slice(0, window - 1).sum())
    if seed is None:
        return pl.Series(values.name, [None] * n, dtype=pl.Float64)

    # The recursion is S_t = S_{t-1}(1 - 1/w) + x_t, which is `w` times an
    # exponential average with alpha = 1/w — so it can ride the same vectorised
    # path as everything else rather than looping.
    tail = values.slice(window - 1, n - window + 1).cast(pl.Float64)
    body = pl.concat([pl.Series([seed / window], dtype=pl.Float64), tail])
    smoothed = body.ewm_mean(alpha=1.0 / window, adjust=False) * float(window)

    # The seed itself is dropped rather than emitted. It is an accumulator over
    # too few observations, not an output — TA-Lib's first value is the one
    # after it, and publishing the seed would put a number where the reference
    # has none.
    head = pl.Series([None] * (window - 1), dtype=pl.Float64)
    return pl.concat([head, smoothed.slice(1)]).rename(values.name or "")


__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "align",
    "as_float",
    "safe_div",
    "seeded_ema",
    "talib_ema",
    "wilder",
    "wilder_sum",
]
