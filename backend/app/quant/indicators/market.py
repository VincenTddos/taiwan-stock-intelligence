"""Market-relative indicators: beta, alpha, correlation, idiosyncratic volatility.

All four come from one rolling regression of the stock's return on the index's,
and all four are computed from rolling moments rather than by fitting a
regression per bar. `beta = (E[xy] - E[x]E[y]) / (E[y²] - E[y]²)` is the OLS
slope exactly, not an approximation, and it costs four rolling means instead of
one least-squares solve per row — which at 2,000 symbols by 2,500 days is the
difference between seconds and an afternoon.

Idiosyncratic volatility uses the same identity: the residual variance of a
single-factor OLS is `var(x)(1 - corr²)`, so it needs no residual series.
"""

from __future__ import annotations

import polars as pl

from app.quant.core import TRADING_DAYS_PER_YEAR
from app.quant.indicators.volatility import ANNUALISATION, log_returns


def _moments(
    x: pl.Series, y: pl.Series, window: int
) -> tuple[pl.Series, pl.Series, pl.Series, pl.Series, pl.Series]:
    """Rolling means of x, y, xy, x², y² over the same window."""
    xf, yf = x.cast(pl.Float64), y.cast(pl.Float64)
    # A null on either side must remove the *pair*, not just one leg, or the
    # covariance would be built from means over different samples.
    paired = pl.DataFrame({"x": xf, "y": yf}).with_columns(
        pl.when(pl.col("x").is_null() | pl.col("y").is_null())
        .then(None)
        .otherwise(pl.col("x"))
        .alias("x"),
        pl.when(pl.col("x").is_null() | pl.col("y").is_null())
        .then(None)
        .otherwise(pl.col("y"))
        .alias("y"),
    )
    xs, ys = paired["x"], paired["y"]
    return (
        xs.rolling_mean(window_size=window),
        ys.rolling_mean(window_size=window),
        (xs * ys).rolling_mean(window_size=window),
        (xs**2).rolling_mean(window_size=window),
        (ys**2).rolling_mean(window_size=window),
    )


def beta(close: pl.Series, benchmark_close: pl.Series, window: int = 60) -> pl.Series:
    """OLS slope of the stock's returns on the benchmark's."""
    x = log_returns(close)
    y = log_returns(benchmark_close)
    mx, my, mxy, _mxx, myy = _moments(x, y, window)
    cov = mxy - mx * my
    var_y = myy - my**2
    return pl.select(
        pl.when(var_y.is_null() | (var_y <= 0)).then(None).otherwise(cov / var_y).alias("v")
    ).to_series()


def alpha(close: pl.Series, benchmark_close: pl.Series, window: int = 60) -> pl.Series:
    """Regression intercept, annualised.

    Annualised by multiplying the daily intercept by the trading year, which is
    the convention; it is not compounded, because the intercept is an average
    daily excess and compounding it would overstate a small number.
    """
    x = log_returns(close)
    y = log_returns(benchmark_close)
    mx, my, mxy, _mxx, myy = _moments(x, y, window)
    cov = mxy - mx * my
    var_y = myy - my**2
    slope = pl.select(
        pl.when(var_y.is_null() | (var_y <= 0)).then(None).otherwise(cov / var_y).alias("v")
    ).to_series()
    return (mx - slope * my) * float(TRADING_DAYS_PER_YEAR)


def correlation(close: pl.Series, benchmark_close: pl.Series, window: int = 60) -> pl.Series:
    x = log_returns(close)
    y = log_returns(benchmark_close)
    mx, my, mxy, mxx, myy = _moments(x, y, window)
    cov = mxy - mx * my
    var_x = mxx - mx**2
    var_y = myy - my**2
    denominator = (var_x * var_y).sqrt()
    return pl.select(
        pl.when(denominator.is_null() | (denominator <= 0))
        .then(None)
        .otherwise(cov / denominator)
        .alias("v")
    ).to_series()


def idiosyncratic_volatility(
    close: pl.Series, benchmark_close: pl.Series, window: int = 60
) -> pl.Series:
    """Annualised volatility of what the market does not explain."""
    x = log_returns(close)
    y = log_returns(benchmark_close)
    mx, my, mxy, mxx, myy = _moments(x, y, window)
    cov = mxy - mx * my
    var_x = mxx - mx**2
    var_y = myy - my**2
    corr_sq = pl.select(
        pl.when(var_x.is_null() | var_y.is_null() | (var_x <= 0) | (var_y <= 0))
        .then(None)
        .otherwise(cov**2 / (var_x * var_y))
        .alias("v")
    ).to_series()
    residual_var = var_x * (1.0 - corr_sq)
    return pl.select(
        pl.when(residual_var.is_null() | (residual_var < 0))
        .then(None)
        .otherwise(residual_var.sqrt() * ANNUALISATION)
        .alias("v")
    ).to_series()


__all__ = ["alpha", "beta", "correlation", "idiosyncratic_volatility"]
