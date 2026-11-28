"""Volume and liquidity indicators.

`vwap` here divides real turnover by real volume, because TWSE publishes both.
The usual textbook VWAP multiplies volume by a "typical price" — the average of
high, low and close — which is a reconstruction of a number the exchange already
knows exactly. Using the reconstruction when the real one is available would
introduce error for no reason.
"""

from __future__ import annotations

import polars as pl

from app.quant.core import align, wilder


def volume_ma(volume: pl.Series, window: int) -> pl.Series:
    return volume.cast(pl.Float64).rolling_mean(window_size=window)


def volume_ratio(volume: pl.Series, window: int) -> pl.Series:
    line = volume_ma(volume, window)
    return pl.select(
        pl.when(line.is_null() | (line == 0))
        .then(None)
        .otherwise(volume.cast(pl.Float64) / line)
        .alias("v")
    ).to_series()


def obv(close: pl.Series, volume: pl.Series) -> pl.Series:
    """On-balance volume: volume signed by the day's direction, accumulated."""
    direction = pl.select(
        # The first bar counts as accumulation. There is no previous close to
        # compare against, and TA-Lib seeds the running total with that day's
        # volume rather than with zero — the level of OBV is arbitrary anyway,
        # but agreeing with the reference is not.
        pl.when(close.cast(pl.Float64).diff().is_null())
        .then(1.0)
        .when(close.cast(pl.Float64).diff() > 0)
        .then(1.0)
        .when(close.cast(pl.Float64).diff() < 0)
        .then(-1.0)
        .otherwise(0.0)
        .alias("v")
    ).to_series()
    return (direction * volume.cast(pl.Float64)).cum_sum()


def obv_slope(close: pl.Series, volume: pl.Series, window: int = 20) -> pl.Series:
    """Change in OBV over the window, scaled by average volume.

    Scaled because raw OBV is an unbounded accumulator whose magnitude depends on
    how long the series is and how heavily the stock trades — comparing it across
    a universe would rank by turnover, not by accumulation.
    """
    line = obv(close, volume)
    change = line - line.shift(window)
    scale = volume_ma(volume, window) * float(window)
    return pl.select(
        pl.when(scale.is_null() | (scale == 0)).then(None).otherwise(change / scale).alias("v")
    ).to_series()


def vwap(turnover: pl.Series, volume: pl.Series, window: int = 20) -> pl.Series:
    """Rolling volume-weighted average price from published turnover."""
    value = turnover.cast(pl.Float64).rolling_sum(window_size=window)
    shares = volume.cast(pl.Float64).rolling_sum(window_size=window)
    return pl.select(
        pl.when(shares.is_null() | (shares == 0)).then(None).otherwise(value / shares).alias("v")
    ).to_series()


def price_vs_vwap(
    close: pl.Series, turnover: pl.Series, volume: pl.Series, window: int = 20
) -> pl.Series:
    line = vwap(turnover, volume, window)
    return pl.select(
        pl.when(line.is_null() | (line == 0))
        .then(None)
        .otherwise(close.cast(pl.Float64) / line - 1.0)
        .alias("v")
    ).to_series()


def mfi(
    high: pl.Series,
    low: pl.Series,
    close: pl.Series,
    volume: pl.Series,
    window: int = 14,
) -> pl.Series:
    """Money Flow Index — RSI weighted by money rather than by price change."""
    typical = (high.cast(pl.Float64) + low.cast(pl.Float64) + close.cast(pl.Float64)) / 3.0
    flow = typical * volume.cast(pl.Float64)
    change = typical.diff()

    positive = pl.select(pl.when(change > 0).then(flow).otherwise(0.0).alias("v")).to_series()
    negative = pl.select(pl.when(change < 0).then(flow).otherwise(0.0).alias("v")).to_series()

    pos = positive.slice(1).rolling_sum(window_size=window)
    neg = negative.slice(1).rolling_sum(window_size=window)
    total = pos + neg
    body = pl.select(
        pl.when(total.is_null() | (total == 0)).then(None).otherwise(100.0 * pos / total).alias("v")
    ).to_series()
    return align(body, close.len())


def volume_breakout(
    close: pl.Series, volume: pl.Series, window: int = 20, multiple: float = 2.0
) -> pl.Series:
    """1 where volume spikes *and* price holds above its average, else 0."""
    ratio = volume_ratio(volume, window)
    line = close.cast(pl.Float64).rolling_mean(window_size=window)
    return pl.select(
        pl.when(ratio.is_null() | line.is_null())
        .then(None)
        .when((ratio > multiple) & (close.cast(pl.Float64) > line))
        .then(1.0)
        .otherwise(0.0)
        .alias("v")
    ).to_series()


def turnover_rate(volume: pl.Series, shares_outstanding: pl.Series) -> pl.Series:
    """Shares traded as a fraction of shares in issue."""
    shares = shares_outstanding.cast(pl.Float64)
    return pl.select(
        pl.when(shares.is_null() | (shares <= 0))
        .then(None)
        .otherwise(volume.cast(pl.Float64) / shares)
        .alias("v")
    ).to_series()


def amihud_illiquidity(close: pl.Series, turnover: pl.Series, window: int = 20) -> pl.Series:
    """Mean of |return| / turnover — price impact per dollar traded.

    Scaled by 1e9 so the values sit in a readable range instead of being
    reported in units where every Taiwanese stock is 1e-9-something.
    """
    prev = close.cast(pl.Float64).shift(1)
    returns = pl.select(
        pl.when(prev.is_null() | (prev == 0))
        .then(None)
        .otherwise((close.cast(pl.Float64) / prev - 1.0).abs())
        .alias("v")
    ).to_series()
    impact = pl.select(
        pl.when(turnover.cast(pl.Float64).is_null() | (turnover.cast(pl.Float64) <= 0))
        .then(None)
        .otherwise(returns / turnover.cast(pl.Float64) * 1e9)
        .alias("v")
    ).to_series()
    return impact.rolling_mean(window_size=window)


def zero_volume_days(volume: pl.Series, window: int = 60) -> pl.Series:
    """Fraction of the window with no trading at all — the crudest liquidity screen."""
    idle = pl.select(
        pl.when(volume.is_null())
        .then(None)
        .when(volume.cast(pl.Float64) == 0)
        .then(1.0)
        .otherwise(0.0)
        .alias("v")
    ).to_series()
    return idle.rolling_mean(window_size=window)


def money_flow_smoothed(
    high: pl.Series, low: pl.Series, close: pl.Series, volume: pl.Series, window: int = 14
) -> pl.Series:
    """Wilder-smoothed typical-price money flow. Kept for the AI score's inputs."""
    typical = (high.cast(pl.Float64) + low.cast(pl.Float64) + close.cast(pl.Float64)) / 3.0
    return wilder((typical * volume.cast(pl.Float64)).slice(1), window)


__all__ = [
    "amihud_illiquidity",
    "mfi",
    "money_flow_smoothed",
    "obv",
    "obv_slope",
    "price_vs_vwap",
    "turnover_rate",
    "volume_breakout",
    "volume_ma",
    "volume_ratio",
    "vwap",
    "zero_volume_days",
]
