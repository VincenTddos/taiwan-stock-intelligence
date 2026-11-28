"""The indicator registry.

Every indicator is declared once, here, with the columns it needs and the
number of bars it needs before it can produce anything. Two things depend on
that declaration and neither could be done with a bare function list:

* the daily job knows how much history to load — the maximum lookback across
  the registry, not a guessed constant that silently truncates `ma_240`
* an indicator whose inputs a panel does not carry is reported as *unavailable*
  rather than being skipped. "This market has no turnover data" and "we forgot
  to compute VWAP" must not look the same on the way out.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import polars as pl

from app.quant.indicators import market, momentum, trend, volatility, volume
from app.quant.panel import Panel

Computation = Callable[[pl.DataFrame], pl.Series]


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """One named output, and everything needed to decide whether it can be produced."""

    name: str
    category: str
    inputs: tuple[str, ...]
    lookback: int
    compute: Computation
    description: str = ""
    params: dict[str, object] = field(default_factory=dict)

    def available(self, columns: Sequence[str]) -> bool:
        return all(column in columns for column in self.inputs)


def _col(frame: pl.DataFrame, name: str) -> pl.Series:
    return frame[name]


def _op(fn: Callable[..., pl.Series], *columns: str, **kwargs: object) -> Computation:
    """Bind an indicator function to the columns it reads.

    A factory rather than `lambda f, n=n: ...`. The late-binding default is the
    usual way to capture a loop variable, and it also makes the lambda's
    signature wider than `Computation`, which mypy declines to check at all —
    so the registry's whole contract went unverified in exactly the place it is
    repeated forty times.
    """

    def compute(frame: pl.DataFrame) -> pl.Series:
        return fn(*(frame[c] for c in columns), **kwargs)

    return compute


def _op_at(
    fn: Callable[..., tuple[pl.Series, ...]], index: int, *columns: str, **kwargs: object
) -> Computation:
    """The same, for functions returning several series at once."""

    def compute(frame: pl.DataFrame) -> pl.Series:
        return fn(*(frame[c] for c in columns), **kwargs)[index]

    return compute


def _specs() -> list[IndicatorSpec]:
    out: list[IndicatorSpec] = []
    ohlc = ("high", "low", "close")

    def add(
        name: str,
        category: str,
        inputs: tuple[str, ...],
        lookback: int,
        compute: Computation,
        description: str = "",
        **params: object,
    ) -> None:
        out.append(
            IndicatorSpec(
                name=name,
                category=category,
                inputs=inputs,
                lookback=lookback,
                compute=compute,
                description=description,
                params=params,
            )
        )

    # ---------------------------------------------------------------- trend
    for n in (5, 10, 20, 60, 120, 240):
        add(
            f"ma_{n}",
            "trend",
            ("close",),
            n,
            _op(trend.sma, "close", window=n),
            f"{n}-day simple moving average",
            window=n,
        )
    for n in (12, 26, 60):
        add(
            f"ema_{n}",
            "trend",
            ("close",),
            n,
            _op(trend.ema, "close", window=n),
            f"{n}-day exponential moving average",
            window=n,
        )
    for n in (20, 60):
        add(
            f"ma_slope_{n}",
            "trend",
            ("close",),
            n + 5,
            _op(trend.ma_slope, "close", window=n),
            f"5-day rate of change of the {n}-day average",
            window=n,
            lag=5,
        )
    for n in (20, 60, 240):
        add(
            f"price_vs_ma_{n}",
            "trend",
            ("close",),
            n,
            _op(trend.price_vs_ma, "close", window=n),
            f"distance from the {n}-day average, as a fraction",
            window=n,
        )
    add(
        "ma_alignment",
        "trend",
        ("close",),
        60,
        lambda f: trend.ma_alignment(_col(f, "close")),
        "+1 bullish stack, -1 bearish, 0 tangled",
    )
    add(
        "adx_14",
        "trend",
        ohlc,
        28,
        lambda f: trend.adx(_col(f, "high"), _col(f, "low"), _col(f, "close"), 14),
        "Wilder trend strength",
        window=14,
    )
    add(
        "di_plus_14",
        "trend",
        ohlc,
        15,
        lambda f: trend.di_plus_minus(_col(f, "high"), _col(f, "low"), _col(f, "close"), 14)[0],
        "Wilder +DI",
        window=14,
    )
    add(
        "di_minus_14",
        "trend",
        ohlc,
        15,
        lambda f: trend.di_plus_minus(_col(f, "high"), _col(f, "low"), _col(f, "close"), 14)[1],
        "Wilder -DI",
        window=14,
    )
    add(
        "aroon_up_25",
        "trend",
        ("high", "low"),
        26,
        lambda f: trend.aroon(_col(f, "high"), _col(f, "low"), 25)[0],
        "bars since the 25-day high",
        window=25,
    )
    add(
        "aroon_down_25",
        "trend",
        ("high", "low"),
        26,
        lambda f: trend.aroon(_col(f, "high"), _col(f, "low"), 25)[1],
        "bars since the 25-day low",
        window=25,
    )
    add(
        "supertrend_10_3",
        "trend",
        ohlc,
        11,
        lambda f: trend.supertrend(_col(f, "high"), _col(f, "low"), _col(f, "close"), 10, 3.0),
        "ATR channel trend direction",
        window=10,
        multiplier=3.0,
    )

    # ------------------------------------------------------------- momentum
    for n in (5, 10, 20, 60, 120):
        add(
            f"roc_{n}",
            "momentum",
            ("close",),
            n,
            _op(momentum.roc, "close", window=n),
            f"{n}-day return",
            window=n,
        )
    for n in (6, 14, 24):
        add(
            f"rsi_{n}",
            "momentum",
            ("close",),
            n + 1,
            _op(momentum.rsi, "close", window=n),
            f"Wilder RSI over {n} days",
            window=n,
        )
    add(
        "macd",
        "momentum",
        ("close",),
        34,
        lambda f: momentum.macd(_col(f, "close"))[0],
        "MACD line",
    )
    add(
        "macd_signal",
        "momentum",
        ("close",),
        34,
        lambda f: momentum.macd(_col(f, "close"))[1],
        "MACD signal line",
    )
    add(
        "macd_hist",
        "momentum",
        ("close",),
        34,
        lambda f: momentum.macd(_col(f, "close"))[2],
        "MACD histogram",
    )
    add(
        "kd_k",
        "momentum",
        ohlc,
        9,
        lambda f: momentum.stochastic_kd(_col(f, "high"), _col(f, "low"), _col(f, "close"))[0],
        "Taiwan KD, K line",
        window=9,
    )
    add(
        "kd_d",
        "momentum",
        ohlc,
        9,
        lambda f: momentum.stochastic_kd(_col(f, "high"), _col(f, "low"), _col(f, "close"))[1],
        "Taiwan KD, D line",
        window=9,
    )
    add(
        "williams_r_14",
        "momentum",
        ohlc,
        14,
        lambda f: momentum.williams_r(_col(f, "high"), _col(f, "low"), _col(f, "close"), 14),
        "Williams %R",
        window=14,
    )
    add(
        "cci_20",
        "momentum",
        ohlc,
        20,
        lambda f: momentum.cci(_col(f, "high"), _col(f, "low"), _col(f, "close"), 20),
        "Commodity Channel Index",
        window=20,
    )
    add(
        "momentum_consistency_60",
        "momentum",
        ("close",),
        61,
        lambda f: momentum.momentum_consistency(_col(f, "close"), 60),
        "fraction of up days in 60 sessions",
        window=60,
    )
    add(
        "high_proximity_250",
        "momentum",
        ("close", "high"),
        250,
        lambda f: momentum.high_proximity(_col(f, "close"), _col(f, "high"), 250),
        "close over the 52-week high",
        window=250,
    )
    for n in (20, 60):
        add(
            f"relative_strength_taiex_{n}",
            "momentum",
            ("close", "benchmark_close"),
            n,
            _op(momentum.relative_strength, "close", "benchmark_close", window=n),
            f"{n}-day return minus the index's",
            window=n,
        )

    # ----------------------------------------------------------- volatility
    add(
        "atr_14",
        "volatility",
        ohlc,
        15,
        lambda f: volatility.atr(_col(f, "high"), _col(f, "low"), _col(f, "close"), 14),
        "Wilder average true range",
        window=14,
    )
    add(
        "atr_pct_14",
        "volatility",
        ohlc,
        15,
        lambda f: volatility.atr_pct(_col(f, "high"), _col(f, "low"), _col(f, "close"), 14),
        "ATR as a fraction of price",
        window=14,
    )
    for n in (20, 60):
        add(
            f"hv_{n}",
            "volatility",
            ("close",),
            n + 1,
            _op(volatility.historical_volatility, "close", window=n),
            f"annualised {n}-day volatility of log returns",
            window=n,
        )
    add(
        "parkinson_hv_20",
        "volatility",
        ("high", "low"),
        20,
        lambda f: volatility.parkinson_volatility(_col(f, "high"), _col(f, "low"), 20),
        "range-based volatility estimator",
        window=20,
    )
    add(
        "bb_upper_20_2",
        "volatility",
        ("close",),
        20,
        lambda f: volatility.bollinger(_col(f, "close"))[0],
        "Bollinger upper band",
    )
    add(
        "bb_mid_20_2",
        "volatility",
        ("close",),
        20,
        lambda f: volatility.bollinger(_col(f, "close"))[1],
        "Bollinger middle band",
    )
    add(
        "bb_lower_20_2",
        "volatility",
        ("close",),
        20,
        lambda f: volatility.bollinger(_col(f, "close"))[2],
        "Bollinger lower band",
    )
    add(
        "bb_width_20",
        "volatility",
        ("close",),
        20,
        lambda f: volatility.bollinger_width(_col(f, "close")),
        "band width over the middle band",
    )
    add(
        "bb_percent_b",
        "volatility",
        ("close",),
        20,
        lambda f: volatility.bollinger_percent_b(_col(f, "close")),
        "position within the bands",
    )
    add(
        "vol_regime",
        "volatility",
        ("close",),
        270,
        lambda f: volatility.volatility_regime(_col(f, "close")),
        "percentile of current volatility in 250 sessions",
    )
    add(
        "downside_deviation_60",
        "volatility",
        ("close",),
        61,
        lambda f: volatility.downside_deviation(_col(f, "close"), 60),
        "annualised volatility of negative returns",
        window=60,
    )
    for n in (60, 250):
        add(
            f"max_drawdown_{n}",
            "volatility",
            ("close",),
            # 2n - 1, not n: the drawdown series itself needs n bars before its
            # first value, and the rolling minimum then needs n of those.
            # Declaring n produced a column of nulls that looked like missing
            # data rather than an under-specified lookback.
            2 * n - 1,
            _op(volatility.max_drawdown, "close", window=n),
            f"worst fall within {n} sessions",
            window=n,
        )

    # --------------------------------------------------------------- volume
    for n in (5, 20, 60):
        add(
            f"volume_ma_{n}",
            "volume",
            ("volume",),
            n,
            _op(volume.volume_ma, "volume", window=n),
            f"{n}-day average volume",
            window=n,
        )
        add(
            f"volume_ratio_{n}",
            "volume",
            ("volume",),
            n,
            _op(volume.volume_ratio, "volume", window=n),
            f"volume over its {n}-day average",
            window=n,
        )
    add(
        "obv",
        "volume",
        ("close", "volume"),
        1,
        lambda f: volume.obv(_col(f, "close"), _col(f, "volume")),
        "on-balance volume",
    )
    add(
        "obv_slope_20",
        "volume",
        ("close", "volume"),
        21,
        lambda f: volume.obv_slope(_col(f, "close"), _col(f, "volume"), 20),
        "scaled 20-day change in OBV",
        window=20,
    )
    add(
        "vwap_20",
        "volume",
        ("turnover", "volume"),
        20,
        lambda f: volume.vwap(_col(f, "turnover"), _col(f, "volume"), 20),
        "20-day VWAP from published turnover",
        window=20,
    )
    add(
        "price_vs_vwap_20",
        "volume",
        ("close", "turnover", "volume"),
        20,
        lambda f: volume.price_vs_vwap(
            _col(f, "close"), _col(f, "turnover"), _col(f, "volume"), 20
        ),
        "distance from VWAP",
        window=20,
    )
    add(
        "mfi_14",
        "volume",
        ("high", "low", "close", "volume"),
        15,
        lambda f: volume.mfi(
            _col(f, "high"), _col(f, "low"), _col(f, "close"), _col(f, "volume"), 14
        ),
        "money flow index",
        window=14,
    )
    add(
        "volume_breakout",
        "volume",
        ("close", "volume"),
        20,
        lambda f: volume.volume_breakout(_col(f, "close"), _col(f, "volume")),
        "volume spike with price above its average",
    )
    add(
        "turnover_rate",
        "volume",
        ("volume", "shares_outstanding"),
        1,
        lambda f: volume.turnover_rate(_col(f, "volume"), _col(f, "shares_outstanding")),
        "volume over shares in issue",
    )
    add(
        "amihud_illiquidity_20",
        "volume",
        ("close", "turnover"),
        21,
        lambda f: volume.amihud_illiquidity(_col(f, "close"), _col(f, "turnover"), 20),
        "price impact per unit traded, scaled 1e9",
        window=20,
    )
    add(
        "zero_volume_days_60",
        "volume",
        ("volume",),
        60,
        lambda f: volume.zero_volume_days(_col(f, "volume"), 60),
        "fraction of sessions with no trading",
        window=60,
    )

    # --------------------------------------------------------------- market
    for n in (60, 250):
        add(
            f"beta_{n}",
            "market",
            ("close", "benchmark_close"),
            n + 1,
            _op(market.beta, "close", "benchmark_close", window=n),
            f"OLS beta to the index over {n} sessions",
            window=n,
        )
        add(
            f"alpha_{n}",
            "market",
            ("close", "benchmark_close"),
            n + 1,
            _op(market.alpha, "close", "benchmark_close", window=n),
            f"annualised regression intercept over {n} sessions",
            window=n,
        )
        add(
            f"corr_taiex_{n}",
            "market",
            ("close", "benchmark_close"),
            n + 1,
            _op(market.correlation, "close", "benchmark_close", window=n),
            f"correlation with the index over {n} sessions",
            window=n,
        )
    add(
        "idio_vol_60",
        "market",
        ("close", "benchmark_close"),
        61,
        lambda f: market.idiosyncratic_volatility(_col(f, "close"), _col(f, "benchmark_close"), 60),
        "annualised volatility of the market-model residual",
        window=60,
    )
    return out


REGISTRY: dict[str, IndicatorSpec] = {spec.name: spec for spec in _specs()}

#: The longest history any indicator needs. The loader reads this rather than a
#: literal, so adding `ma_480` cannot quietly produce a column of nulls.
MAX_LOOKBACK: int = max(spec.lookback for spec in REGISTRY.values())


def available_specs(columns: Sequence[str]) -> list[IndicatorSpec]:
    return [spec for spec in REGISTRY.values() if spec.available(columns)]


def unavailable_specs(columns: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Indicator name -> the columns it wanted and the panel did not have."""
    return {
        spec.name: tuple(c for c in spec.inputs if c not in columns)
        for spec in REGISTRY.values()
        if not spec.available(columns)
    }


def compute(panel: Panel, names: Sequence[str] | None = None) -> pl.DataFrame:
    """Every available indicator for every symbol in the panel.

    Returns one row per (symbol, trading_date) with one column per indicator.
    Symbols are computed independently — a rolling window must never see the
    next symbol's first bar, which is exactly what a single frame-wide
    `rolling_mean` would do.
    """
    frame = panel.frame
    if frame.is_empty():
        return pl.DataFrame({"symbol": [], "trading_date": []})

    wanted = (
        [REGISTRY[name] for name in names] if names is not None else available_specs(frame.columns)
    )
    usable = [spec for spec in wanted if spec.available(frame.columns)]

    parts: list[pl.DataFrame] = []
    for (_symbol,), group in frame.group_by(["symbol"], maintain_order=True):
        columns: dict[str, pl.Series] = {
            "symbol": group["symbol"],
            "trading_date": group["trading_date"],
        }
        for spec in usable:
            values = spec.compute(group)
            columns[spec.name] = values.rename(spec.name).cast(pl.Float64)
        parts.append(pl.DataFrame(columns))
    return pl.concat(parts, how="vertical") if parts else pl.DataFrame()


__all__ = [
    "MAX_LOOKBACK",
    "REGISTRY",
    "IndicatorSpec",
    "available_specs",
    "compute",
    "unavailable_specs",
]
