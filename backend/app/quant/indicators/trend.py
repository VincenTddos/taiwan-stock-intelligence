"""Trend indicators.

Wilder's ADX is the awkward one and is worth a note. It smooths the directional
movements with a running *sum* rather than an average, takes their ratio, then
smooths the result again — so the seeding of each stage survives into the
output and cannot be waved away as a transient. TA-Lib's ADX is only defined
from bar `2n - 1`; that is reproduced here rather than filled in, because a
value produced from fewer bars than the indicator needs is not a smaller-sample
estimate, it is a different number wearing the same name.
"""

from __future__ import annotations

import polars as pl

from app.quant.core import align, talib_ema, wilder, wilder_sum


def sma(close: pl.Series, window: int) -> pl.Series:
    return close.cast(pl.Float64).rolling_mean(window_size=window)


def ema(close: pl.Series, window: int) -> pl.Series:
    return talib_ema(close, window)


def ma_slope(close: pl.Series, window: int, lag: int = 5) -> pl.Series:
    """Rate of change of the moving average itself, over `lag` bars."""
    line = sma(close, window)
    past = line.shift(lag)
    return pl.select(
        pl.when(past.is_null() | (past == 0)).then(None).otherwise((line - past) / past).alias("v")
    ).to_series()


def price_vs_ma(close: pl.Series, window: int) -> pl.Series:
    """How far price sits above or below its average, as a fraction."""
    line = sma(close, window)
    return pl.select(
        pl.when(line.is_null() | (line == 0))
        .then(None)
        .otherwise((close.cast(pl.Float64) - line) / line)
        .alias("v")
    ).to_series()


def ma_alignment(close: pl.Series, fast: int = 5, mid: int = 20, slow: int = 60) -> pl.Series:
    """+1 bullish stack, -1 bearish stack, 0 tangled.

    Ternary rather than boolean because "tangled" is the common state and is
    informative — collapsing it into "not bullish" would make a sideways market
    look like a downtrend.
    """
    f, m, s = sma(close, fast), sma(close, mid), sma(close, slow)
    return pl.select(
        pl.when(f.is_null() | m.is_null() | s.is_null())
        .then(None)
        .when((f > m) & (m > s))
        .then(1.0)
        .when((f < m) & (m < s))
        .then(-1.0)
        .otherwise(0.0)
        .alias("v")
    ).to_series()


def true_range(high: pl.Series, low: pl.Series, close: pl.Series) -> pl.Series:
    """max(H-L, |H-C_prev|, |L-C_prev|). First bar is null, as TA-Lib has it."""
    prev = close.cast(pl.Float64).shift(1)
    tr = pl.select(
        pl.max_horizontal(
            high.cast(pl.Float64) - low.cast(pl.Float64),
            (high.cast(pl.Float64) - prev).abs(),
            (low.cast(pl.Float64) - prev).abs(),
        ).alias("v")
    ).to_series()
    # `max_horizontal` skips nulls, so the first bar would otherwise come back
    # as the high-low range — a true range computed without a previous close,
    # which is not the same quantity.
    return pl.select(pl.when(prev.is_null()).then(None).otherwise(tr).alias("v")).to_series()


def _directional_movement(high: pl.Series, low: pl.Series) -> tuple[pl.Series, pl.Series]:
    up = high.cast(pl.Float64).diff()
    down = -low.cast(pl.Float64).diff()
    plus = pl.select(pl.when((up > down) & (up > 0)).then(up).otherwise(0.0).alias("v")).to_series()
    minus = pl.select(
        pl.when((down > up) & (down > 0)).then(down).otherwise(0.0).alias("v")
    ).to_series()
    return plus, minus


def di_plus_minus(
    high: pl.Series, low: pl.Series, close: pl.Series, window: int = 14
) -> tuple[pl.Series, pl.Series]:
    """Wilder's +DI and -DI, in percent."""
    plus_dm, minus_dm = _directional_movement(high, low)
    tr = true_range(high, low, close)

    # Drop the leading null so the accumulation starts on real values, then put
    # it back. `wilder_sum`, not `wilder`: see its docstring for why the two
    # differ and why the difference survives into the ratio.
    tr_s = wilder_sum(tr.slice(1), window)
    plus_s = wilder_sum(plus_dm.slice(1), window)
    minus_s = wilder_sum(minus_dm.slice(1), window)

    def _di(dm: pl.Series) -> pl.Series:
        ratio = pl.select(
            pl.when(tr_s.is_null() | (tr_s == 0)).then(None).otherwise(100.0 * dm / tr_s).alias("v")
        ).to_series()
        return align(ratio, high.len())

    return _di(plus_s), _di(minus_s)


def adx(high: pl.Series, low: pl.Series, close: pl.Series, window: int = 14) -> pl.Series:
    """Wilder's ADX: the smoothed directional index."""
    plus_di, minus_di = di_plus_minus(high, low, close, window)
    total = plus_di + minus_di
    dx = pl.select(
        pl.when(total.is_null() | (total == 0))
        .then(None)
        .otherwise(100.0 * (plus_di - minus_di).abs() / total)
        .alias("v")
    ).to_series()

    # DX is null until bar `window`; ADX is its Wilder average, so it needs a
    # further `window` bars of DX before it exists at all.
    smoothed = wilder(dx.slice(window), window)
    return align(smoothed, high.len())


def aroon(high: pl.Series, low: pl.Series, window: int = 25) -> tuple[pl.Series, pl.Series]:
    """Bars since the window's high and low, as percentages.

    TA-Lib measures over `window + 1` bars — the lookback plus today — so the
    same convention is used here rather than the one that reads more naturally.
    """
    span = window + 1
    h = high.cast(pl.Float64)
    low_f = low.cast(pl.Float64)
    idx_high = h.rolling_map(lambda s: float(s.arg_max() or 0), window_size=span)
    idx_low = low_f.rolling_map(lambda s: float(s.arg_min() or 0), window_size=span)
    up = 100.0 * idx_high / window
    down = 100.0 * idx_low / window
    return up, down


def supertrend(
    high: pl.Series,
    low: pl.Series,
    close: pl.Series,
    window: int = 10,
    multiplier: float = 3.0,
) -> pl.Series:
    """+1 while the ATR channel says uptrend, -1 while it says down.

    The band is path dependent — it ratchets and only flips when price closes
    through it — so this one is an honest loop. It runs per symbol over a few
    thousand bars, which is affordable; what is not affordable is a loop inside
    something that runs for every indicator.
    """
    atr = align(wilder(true_range(high, low, close).slice(1), window), close.len())

    hl2 = (high.cast(pl.Float64) + low.cast(pl.Float64)) / 2.0
    upper = (hl2 + multiplier * atr).to_list()
    lower = (hl2 - multiplier * atr).to_list()
    closes = close.cast(pl.Float64).to_list()

    out: list[float | None] = []
    direction = 1
    final_upper: float | None = None
    final_lower: float | None = None

    for i in range(len(closes)):
        u, low_band, c = upper[i], lower[i], closes[i]
        if u is None or low_band is None or c is None:
            out.append(None)
            continue
        prev_close = closes[i - 1] if i else None
        if final_upper is None or prev_close is None:
            final_upper, final_lower = u, low_band
        else:
            final_upper = u if (u < final_upper or prev_close > final_upper) else final_upper
            final_lower = (
                low_band if (low_band > final_lower or prev_close < final_lower) else final_lower
            )
        if final_upper is not None and c > final_upper:
            direction = 1
        elif final_lower is not None and c < final_lower:
            direction = -1
        out.append(float(direction))
    return pl.Series("supertrend", out, dtype=pl.Float64)


__all__ = [
    "adx",
    "aroon",
    "di_plus_minus",
    "ema",
    "ma_alignment",
    "ma_slope",
    "price_vs_ma",
    "sma",
    "supertrend",
    "true_range",
]
