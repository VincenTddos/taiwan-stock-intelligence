"""Momentum indicators.

One deliberate departure from the golden reference lives here. `kd_k` / `kd_d`
are the Taiwanese convention — RSV smoothed by `1/3` exponential steps — not
TA-Lib's `STOCH`, which smooths with simple moving averages. They are different
indicators that share a name, and every Taiwanese chart, textbook and broker
platform shows the former. Matching TA-Lib here would produce a number nobody
in this market would recognise, so `kd` is tested against a hand-computed
vector instead, and the reason is recorded rather than left as a failing
comparison someone would eventually "fix".
"""

from __future__ import annotations

import polars as pl

from app.quant.core import align, as_float, talib_ema, wilder


def roc(close: pl.Series, window: int) -> pl.Series:
    """Rate of change as a fraction: `close_t / close_{t-n} - 1`.

    A fraction, not TA-Lib's percentage. Every downstream consumer — factor
    standardisation, return series, the API — works in fractions, and having one
    indicator arrive multiplied by a hundred is the kind of unit mismatch that
    survives review and shows up as a factor that is inexplicably strong.
    """
    past = close.cast(pl.Float64).shift(window)
    return pl.select(
        pl.when(past.is_null() | (past == 0))
        .then(None)
        .otherwise(close.cast(pl.Float64) / past - 1.0)
        .alias("v")
    ).to_series()


def rsi(close: pl.Series, window: int = 14) -> pl.Series:
    """Wilder's RSI."""
    delta = close.cast(pl.Float64).diff()
    gain = pl.select(pl.when(delta > 0).then(delta).otherwise(0.0).alias("v")).to_series()
    loss = pl.select(pl.when(delta < 0).then(-delta).otherwise(0.0).alias("v")).to_series()

    avg_gain = wilder(gain.slice(1), window)
    avg_loss = wilder(loss.slice(1), window)
    total = avg_gain + avg_loss
    body = pl.select(
        pl.when(total.is_null() | (total == 0))
        .then(None)
        .otherwise(100.0 * avg_gain / total)
        .alias("v")
    ).to_series()
    return align(body, close.len())


def macd(
    close: pl.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pl.Series, pl.Series, pl.Series]:
    """MACD line, signal, histogram.

    Two things here are not obvious and both come from the reference.

    The fast EMA is seeded at the slow EMA's start — from the twelve bars ending
    there, not from the first twelve of the series. TA-Lib starts every internal
    average at the index its caller needs, and seeding the fast leg the
    "natural" way puts the MACD line out by about 0.2 for the first hundred
    bars: converging, plausible, and never equal.

    The line is published only from where the signal exists, even though the
    subtraction is defined earlier. That is not tidiness. Because the fast leg's
    seed depends on the slow period, and the signal's start depends on the
    signal period, a MACD line emitted before the signal would take different
    values depending on which signal period was requested — the same series
    quietly meaning different things.
    """
    slow_start = slow - 1
    fast_line = talib_ema(close, fast, start=slow_start)
    slow_line = talib_ema(close, slow)
    line = fast_line - slow_line

    sig = align(talib_ema(line.slice(slow_start), signal), close.len())

    published_from = slow_start + signal - 1
    mask = pl.Series([i >= published_from for i in range(close.len())])
    line = pl.select(pl.when(mask).then(line).otherwise(None).alias("v")).to_series()
    return line, sig, line - sig


def stochastic_kd(
    high: pl.Series,
    low: pl.Series,
    close: pl.Series,
    window: int = 9,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> tuple[pl.Series, pl.Series]:
    """Taiwan's KD.

    `RSV = (C - min(L, n)) / (max(H, n) - min(L, n)) * 100`, then
    `K = K_prev + (RSV - K_prev) / k_smooth` and `D = D_prev + (K - D_prev) /
    d_smooth`, both seeded at 50 — which is the convention every Taiwanese
    platform uses, and the reason this is not TA-Lib's STOCH.
    """
    h = high.cast(pl.Float64).rolling_max(window_size=window)
    low_roll = low.cast(pl.Float64).rolling_min(window_size=window)
    span = h - low_roll
    rsv = pl.select(
        # Two different cases that were briefly conflated. A window that is not
        # yet full is *unknown* and must be null — a stock listed three days ago
        # has no nine-day KD, and 50 would be a fabricated neutral reading. A
        # window that is full but flat has no position within it, and 50 is the
        # only non-arbitrary answer there, which is what the platforms show.
        pl.when(span.is_null())
        .then(None)
        .when(span == 0)
        .then(50.0)
        .otherwise(100.0 * (close.cast(pl.Float64) - low_roll) / span)
        .alias("v")
    ).to_series()

    k_values: list[float | None] = []
    d_values: list[float | None] = []
    k_prev = d_prev = 50.0
    for value in rsv.to_list():
        if value is None:
            k_values.append(None)
            d_values.append(None)
            continue
        k_prev = k_prev + (value - k_prev) / k_smooth
        d_prev = d_prev + (k_prev - d_prev) / d_smooth
        k_values.append(k_prev)
        d_values.append(d_prev)
    return (
        pl.Series("kd_k", k_values, dtype=pl.Float64),
        pl.Series("kd_d", d_values, dtype=pl.Float64),
    )


def williams_r(high: pl.Series, low: pl.Series, close: pl.Series, window: int = 14) -> pl.Series:
    h = high.cast(pl.Float64).rolling_max(window_size=window)
    low_roll = low.cast(pl.Float64).rolling_min(window_size=window)
    span = h - low_roll
    return pl.select(
        pl.when(span.is_null() | (span == 0))
        .then(None)
        .otherwise(-100.0 * (h - close.cast(pl.Float64)) / span)
        .alias("v")
    ).to_series()


def _mean_absolute_deviation(values: pl.Series) -> float:
    """Mean |x - mean(x)|. A named function so it can be typed; CCI's deviation
    is the mean absolute deviation, not the standard deviation."""
    mean = as_float(values.mean()) or 0.0
    return as_float((values - mean).abs().mean()) or 0.0


def cci(high: pl.Series, low: pl.Series, close: pl.Series, window: int = 20) -> pl.Series:
    """Commodity Channel Index.

    The deviation is the *mean* absolute deviation, not the standard deviation.
    Using the standard deviation is the usual mistake and produces a series that
    correlates strongly with the right answer while never equalling it.
    """
    typical = (high.cast(pl.Float64) + low.cast(pl.Float64) + close.cast(pl.Float64)) / 3.0
    mean = typical.rolling_mean(window_size=window)
    frame = pl.DataFrame({"tp": typical})
    mad = frame.select(
        pl.col("tp").rolling_map(_mean_absolute_deviation, window_size=window).alias("mad")
    ).to_series()
    return pl.select(
        pl.when(mad.is_null() | (mad == 0))
        .then(None)
        .otherwise((typical - mean) / (0.015 * mad))
        .alias("v")
    ).to_series()


def relative_strength(close: pl.Series, benchmark_close: pl.Series, window: int) -> pl.Series:
    """Stock return minus benchmark return over the same window.

    A difference of returns, not a ratio of prices: a ratio embeds the arbitrary
    level of the index and is not comparable across periods.
    """
    return roc(close, window) - roc(benchmark_close, window)


def momentum_consistency(close: pl.Series, window: int = 60) -> pl.Series:
    """Fraction of the last `window` sessions that closed up."""
    up = pl.select(
        pl.when(close.cast(pl.Float64).diff().is_null())
        .then(None)
        .when(close.cast(pl.Float64).diff() > 0)
        .then(1.0)
        .otherwise(0.0)
        .alias("v")
    ).to_series()
    return up.rolling_mean(window_size=window)


def high_proximity(close: pl.Series, high: pl.Series, window: int = 250) -> pl.Series:
    """`close / max(high, window)` — 1.0 means sitting on the 52-week high."""
    peak = high.cast(pl.Float64).rolling_max(window_size=window)
    return pl.select(
        pl.when(peak.is_null() | (peak == 0))
        .then(None)
        .otherwise(close.cast(pl.Float64) / peak)
        .alias("v")
    ).to_series()


__all__ = [
    "cci",
    "high_proximity",
    "macd",
    "momentum_consistency",
    "relative_strength",
    "roc",
    "rsi",
    "stochastic_kd",
    "williams_r",
]
