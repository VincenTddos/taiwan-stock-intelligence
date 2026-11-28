"""Volatility indicators.

`hv` annualises with 252 rather than 365. Volatility scales with the number of
*trading* observations, and Taiwan trades about 243 days a year — using calendar
days would inflate every volatility figure by about 22% and, worse, would make
this platform's numbers incomparable with every published one.
"""

from __future__ import annotations

import math

import polars as pl

from app.quant.core import TRADING_DAYS_PER_YEAR, align, wilder
from app.quant.indicators.trend import true_range

ANNUALISATION = math.sqrt(TRADING_DAYS_PER_YEAR)


def atr(high: pl.Series, low: pl.Series, close: pl.Series, window: int = 14) -> pl.Series:
    """Wilder's ATR.

    Seeded with the simple mean of the first `window` true ranges — which is one
    more observation than the directional indicators use for the same input. The
    asymmetry is TA-Lib's, not a slip here.
    """
    tr = true_range(high, low, close)
    return align(wilder(tr.slice(1), window), close.len())


def atr_pct(high: pl.Series, low: pl.Series, close: pl.Series, window: int = 14) -> pl.Series:
    band = atr(high, low, close, window)
    return pl.select(
        pl.when(close.cast(pl.Float64) == 0)
        .then(None)
        .otherwise(band / close.cast(pl.Float64))
        .alias("v")
    ).to_series()


def log_returns(close: pl.Series) -> pl.Series:
    prev = close.cast(pl.Float64).shift(1)
    return pl.select(
        pl.when(prev.is_null() | (prev <= 0) | (close.cast(pl.Float64) <= 0))
        .then(None)
        .otherwise((close.cast(pl.Float64) / prev).log())
        .alias("v")
    ).to_series()


def historical_volatility(close: pl.Series, window: int = 20) -> pl.Series:
    """Annualised standard deviation of log returns."""
    return log_returns(close).rolling_std(window_size=window, ddof=1) * ANNUALISATION


def parkinson_volatility(high: pl.Series, low: pl.Series, window: int = 20) -> pl.Series:
    """Annualised volatility from the high-low range.

    Roughly five times more efficient than the close-to-close estimator for the
    same window, because it uses the path the price took rather than only where
    it stopped. It underestimates when there are overnight gaps, which in a
    market with a 10% daily limit is a real and directional bias — so it is
    reported alongside `hv`, never instead of it.
    """
    ratio = pl.select(
        pl.when((low.cast(pl.Float64) <= 0) | (high.cast(pl.Float64) <= 0))
        .then(None)
        .otherwise((high.cast(pl.Float64) / low.cast(pl.Float64)).log() ** 2)
        .alias("v")
    ).to_series()
    mean_sq = ratio.rolling_mean(window_size=window)
    return (mean_sq / (4.0 * math.log(2.0))).sqrt() * ANNUALISATION


def bollinger(
    close: pl.Series, window: int = 20, deviations: float = 2.0
) -> tuple[pl.Series, pl.Series, pl.Series]:
    """Upper, middle, lower.

    The deviation is the population standard deviation (`ddof=0`), which is what
    TA-Lib uses. The sample version is defensible statistically and produces a
    visibly different band, so the reference wins.
    """
    mid = close.cast(pl.Float64).rolling_mean(window_size=window)
    sd = close.cast(pl.Float64).rolling_std(window_size=window, ddof=0)
    return mid + deviations * sd, mid, mid - deviations * sd


def bollinger_width(close: pl.Series, window: int = 20, deviations: float = 2.0) -> pl.Series:
    upper, mid, lower = bollinger(close, window, deviations)
    return pl.select(
        pl.when(mid.is_null() | (mid == 0)).then(None).otherwise((upper - lower) / mid).alias("v")
    ).to_series()


def bollinger_percent_b(close: pl.Series, window: int = 20, deviations: float = 2.0) -> pl.Series:
    upper, _mid, lower = bollinger(close, window, deviations)
    span = upper - lower
    return pl.select(
        pl.when(span.is_null() | (span == 0))
        .then(None)
        .otherwise((close.cast(pl.Float64) - lower) / span)
        .alias("v")
    ).to_series()


def volatility_regime(close: pl.Series, window: int = 20, lookback: int = 250) -> pl.Series:
    """Where today's volatility sits in its own recent history, in [0, 1].

    A percentile rather than a level, because "high volatility" for a utility and
    for a small-cap electronics maker are different numbers and comparing the
    levels across a universe compares sectors rather than states.
    """
    hv = historical_volatility(close, window)
    frame = pl.DataFrame({"hv": hv})
    return frame.select(
        pl.col("hv")
        .rolling_map(
            lambda s: float((s <= s[-1]).sum()) / float(s.len()) if s[-1] is not None else None,
            window_size=lookback,
            min_samples=lookback,
        )
        .alias("v")
    ).to_series()


def downside_deviation(close: pl.Series, window: int = 60) -> pl.Series:
    """Annualised standard deviation of negative returns only.

    Zeroed rather than dropped on up days: dropping them would change the sample
    size with the market's direction, so a calm rally and a quiet market would
    produce the same number for different reasons.
    """
    returns = log_returns(close)
    negative = pl.select(
        pl.when(returns.is_null())
        .then(None)
        .when(returns < 0)
        .then(returns)
        .otherwise(0.0)
        .alias("v")
    ).to_series()
    mean_sq = (negative**2).rolling_mean(window_size=window)
    return mean_sq.sqrt() * ANNUALISATION


def max_drawdown(close: pl.Series, window: int = 250) -> pl.Series:
    """Worst peak-to-trough fall within the trailing window, as a negative number."""
    peak = close.cast(pl.Float64).rolling_max(window_size=window)
    drawdown = pl.select(
        pl.when(peak.is_null() | (peak == 0))
        .then(None)
        .otherwise(close.cast(pl.Float64) / peak - 1.0)
        .alias("v")
    ).to_series()
    return drawdown.rolling_min(window_size=window)


__all__ = [
    "ANNUALISATION",
    "atr",
    "atr_pct",
    "bollinger",
    "bollinger_percent_b",
    "bollinger_width",
    "downside_deviation",
    "historical_volatility",
    "log_returns",
    "max_drawdown",
    "parkinson_volatility",
    "volatility_regime",
]
