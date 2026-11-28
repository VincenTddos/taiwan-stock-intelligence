"""Golden tests: every indicator TA-Lib defines must equal TA-Lib's answer.

`rtol=1e-6`, which is tight enough that a difference in *seeding* fails — and
seeding is where these implementations actually diverge. A moving average is
hard to get wrong; a Wilder average seeded with the wrong number of observations
converges towards the right answer and never reaches it, which looks correct on
a chart and is wrong in every backtest.

Three indicators are deliberately not compared against TA-Lib, and each says why
at its own test. The rule is that a departure from the reference must be a
decision with a reason attached, never a tolerance quietly widened.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from app.quant.indicators import market, momentum, trend, volatility, volume

talib = pytest.importorskip(
    "talib",
    reason=(
        "TA-Lib is the golden reference for the indicator library. Without it these "
        "tests cannot run — which is a gap in the environment, not a passing suite."
    ),
)

RTOL = 1e-6


@pytest.fixture(scope="module")
def bars() -> dict[str, np.ndarray]:
    """A deterministic random walk with a plausible intraday range.

    Random rather than recorded because the point is to exercise the arithmetic
    over many regimes; deterministic because a golden test that changes its
    input between runs cannot be debugged.
    """
    rng = np.random.default_rng(20260819)
    n = 600
    close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.018, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.009, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.009, n)))
    volume_series = rng.integers(1_000, 5_000_000, n).astype(float)
    return {"close": close, "high": high, "low": low, "volume": volume_series}


def _series(values: np.ndarray) -> pl.Series:
    return pl.Series(values)


def assert_matches(ours: pl.Series, theirs: np.ndarray, *, rtol: float = RTOL) -> None:
    """Same values, and the same idea of where the indicator begins.

    The alignment half matters as much as the values. An implementation that
    publishes a number one bar before the reference does is one that computed it
    from fewer observations than the definition requires.
    """
    mine = np.array(ours.to_numpy(), dtype=float)
    reference = np.asarray(theirs, dtype=float)
    assert mine.shape == reference.shape

    reference_missing = np.isnan(reference)
    assert np.all(np.isnan(mine[reference_missing])), (
        "produced a value where the reference has none — the indicator starts too early"
    )
    np.testing.assert_allclose(mine[~reference_missing], reference[~reference_missing], rtol=rtol)


class TestTrend:
    @pytest.mark.parametrize("window", [5, 20, 60, 240])
    def test_sma(self, bars, window):
        assert_matches(trend.sma(_series(bars["close"]), window), talib.SMA(bars["close"], window))

    @pytest.mark.parametrize("window", [12, 26, 60])
    def test_ema(self, bars, window):
        assert_matches(trend.ema(_series(bars["close"]), window), talib.EMA(bars["close"], window))

    def test_true_range(self, bars):
        assert_matches(
            trend.true_range(_series(bars["high"]), _series(bars["low"]), _series(bars["close"])),
            talib.TRANGE(bars["high"], bars["low"], bars["close"]),
        )

    @pytest.mark.parametrize("window", [14, 20])
    def test_directional_indicators(self, bars, window):
        plus, minus = trend.di_plus_minus(
            _series(bars["high"]), _series(bars["low"]), _series(bars["close"]), window
        )
        assert_matches(plus, talib.PLUS_DI(bars["high"], bars["low"], bars["close"], window))
        assert_matches(minus, talib.MINUS_DI(bars["high"], bars["low"], bars["close"], window))

    @pytest.mark.parametrize("window", [14, 20])
    def test_adx(self, bars, window):
        assert_matches(
            trend.adx(_series(bars["high"]), _series(bars["low"]), _series(bars["close"]), window),
            talib.ADX(bars["high"], bars["low"], bars["close"], window),
        )

    def test_aroon(self, bars):
        up, down = trend.aroon(_series(bars["high"]), _series(bars["low"]), 25)
        talib_down, talib_up = talib.AROON(bars["high"], bars["low"], 25)
        assert_matches(up, talib_up)
        assert_matches(down, talib_down)


class TestMomentum:
    @pytest.mark.parametrize("window", [5, 10, 20, 60, 120])
    def test_roc(self, bars, window):
        """TA-Lib reports a percentage; this library reports a fraction.

        The comparison divides by a hundred rather than the library multiplying
        by it: every consumer downstream — returns, factor standardisation, the
        API — works in fractions, and one indicator arriving a hundred times too
        large is the kind of unit error that survives review.
        """
        assert_matches(
            momentum.roc(_series(bars["close"]), window),
            talib.ROC(bars["close"], window) / 100.0,
        )

    @pytest.mark.parametrize("window", [6, 14, 24])
    def test_rsi(self, bars, window):
        assert_matches(
            momentum.rsi(_series(bars["close"]), window), talib.RSI(bars["close"], window)
        )

    def test_macd(self, bars):
        line, signal, hist = momentum.macd(_series(bars["close"]))
        talib_line, talib_signal, talib_hist = talib.MACD(bars["close"], 12, 26, 9)
        assert_matches(line, talib_line)
        assert_matches(signal, talib_signal)
        assert_matches(hist, talib_hist)

    def test_williams_r(self, bars):
        assert_matches(
            momentum.williams_r(
                _series(bars["high"]), _series(bars["low"]), _series(bars["close"]), 14
            ),
            talib.WILLR(bars["high"], bars["low"], bars["close"], 14),
        )

    def test_cci(self, bars):
        assert_matches(
            momentum.cci(_series(bars["high"]), _series(bars["low"]), _series(bars["close"]), 20),
            talib.CCI(bars["high"], bars["low"], bars["close"], 20),
        )


class TestVolatilityAndVolume:
    @pytest.mark.parametrize("window", [14, 20])
    def test_atr(self, bars, window):
        assert_matches(
            volatility.atr(
                _series(bars["high"]), _series(bars["low"]), _series(bars["close"]), window
            ),
            talib.ATR(bars["high"], bars["low"], bars["close"], window),
        )

    def test_bollinger(self, bars):
        upper, mid, lower = volatility.bollinger(_series(bars["close"]), 20, 2.0)
        t_upper, t_mid, t_lower = talib.BBANDS(bars["close"], 20, 2.0, 2.0)
        assert_matches(upper, t_upper)
        assert_matches(mid, t_mid)
        assert_matches(lower, t_lower)

    def test_obv(self, bars):
        assert_matches(
            volume.obv(_series(bars["close"]), _series(bars["volume"])),
            talib.OBV(bars["close"], bars["volume"]),
        )

    def test_mfi(self, bars):
        assert_matches(
            volume.mfi(
                _series(bars["high"]),
                _series(bars["low"]),
                _series(bars["close"]),
                _series(bars["volume"]),
                14,
            ),
            talib.MFI(bars["high"], bars["low"], bars["close"], bars["volume"], 14),
        )


class TestDeliberateDepartures:
    """Where this library does not match TA-Lib, and why."""

    def test_kd_follows_the_taiwanese_convention_not_talib_stoch(self, bars):
        """Taiwan's KD smooths RSV by thirds; TA-Lib's STOCH uses moving averages.

        They are different indicators sharing a name. Every Taiwanese chart,
        textbook and broker platform shows the former, so matching TA-Lib here
        would produce a number nobody in this market would recognise. The
        recursion is checked directly instead.
        """
        high, low, close = (_series(bars[k]) for k in ("high", "low", "close"))
        k_line, d_line = momentum.stochastic_kd(high, low, close, 9, 3, 3)

        window_high = bars["high"][-9:].max()
        window_low = bars["low"][-9:].min()
        rsv = 100.0 * (bars["close"][-1] - window_low) / (window_high - window_low)

        k_prev = k_line[-2]
        d_prev = d_line[-2]
        assert k_line[-1] == pytest.approx(k_prev + (rsv - k_prev) / 3, rel=1e-12)
        assert d_line[-1] == pytest.approx(d_prev + (k_line[-1] - d_prev) / 3, rel=1e-12)

        stoch_k, _stoch_d = talib.STOCH(bars["high"], bars["low"], bars["close"], 9, 3, 0, 3, 0)
        assert k_line[-1] != pytest.approx(stoch_k[-1], rel=1e-3), (
            "if these agreed, one of the two conventions would be misimplemented"
        )

    def test_beta_matches_an_independent_least_squares_fit(self, bars):
        """No TA-Lib equivalent, so the reference is numpy's own regression.

        `market.beta` computes the slope from rolling moments rather than by
        fitting per bar — an identity, not an approximation, and this is what
        establishes that.
        """
        rng = np.random.default_rng(7)
        benchmark = 10_000 * np.exp(np.cumsum(rng.normal(0, 0.01, bars["close"].size)))
        window = 60
        ours = market.beta(_series(bars["close"]), _series(benchmark), window)

        stock_returns = np.diff(np.log(bars["close"]), prepend=np.nan)
        index_returns = np.diff(np.log(benchmark), prepend=np.nan)
        at = 300
        x = stock_returns[at - window + 1 : at + 1]
        y = index_returns[at - window + 1 : at + 1]
        slope, _intercept = np.polyfit(y, x, 1)
        assert ours[at] == pytest.approx(slope, rel=1e-9)

    def test_idiosyncratic_volatility_matches_the_residual_standard_deviation(self, bars):
        rng = np.random.default_rng(7)
        benchmark = 10_000 * np.exp(np.cumsum(rng.normal(0, 0.01, bars["close"].size)))
        window = 60
        ours = market.idiosyncratic_volatility(_series(bars["close"]), _series(benchmark), window)

        stock_returns = np.diff(np.log(bars["close"]), prepend=np.nan)
        index_returns = np.diff(np.log(benchmark), prepend=np.nan)
        at = 300
        x = stock_returns[at - window + 1 : at + 1]
        y = index_returns[at - window + 1 : at + 1]
        slope, intercept = np.polyfit(y, x, 1)
        residual = x - (intercept + slope * y)
        expected = residual.std(ddof=0) * np.sqrt(252)
        assert ours[at] == pytest.approx(expected, rel=1e-9)


class TestSeedingIsTheHardPart:
    def test_a_naive_wilder_average_would_not_pass(self, bars):
        """The failure this suite exists to catch, demonstrated.

        Starting the recursion at the first observation instead of seeding it
        with a simple mean gives a series that tracks the reference closely and
        differs everywhere — the exact shape of error a loose tolerance hides.
        """
        close = _series(bars["close"])
        correct = trend.ema(close, 26)
        naive = close.ewm_mean(alpha=2 / 27, adjust=False)

        reference = talib.EMA(bars["close"], 26)
        valid = ~np.isnan(reference)
        naive_values = np.array(naive.to_numpy(), dtype=float)[valid]
        correct_values = np.array(correct.to_numpy(), dtype=float)[valid]

        np.testing.assert_allclose(correct_values, reference[valid], rtol=RTOL)
        assert not np.allclose(naive_values, reference[valid], rtol=RTOL)
        # Close enough to be mistaken for correct, which is the whole problem.
        assert np.allclose(naive_values, reference[valid], rtol=1e-2)


class TestShortHistory:
    """A newly listed company has fewer bars than most indicators need.

    `QUANT_ENGINE.md` §1.3 is explicit: an indicator whose window is not yet
    filled returns null, and a shorter window is never substituted. The property
    that makes that safe is that every indicator returns exactly one value per
    input bar — which several did not when the warm-up padding exceeded the
    input, and which only showed up when a real session with one bar per symbol
    was computed.
    """

    @pytest.mark.parametrize("bars_available", [1, 2, 5, 30, 100])
    def test_every_indicator_returns_one_value_per_bar(self, bars, bars_available):
        from app.quant import indicators as registry
        from app.quant import panel as panel_mod

        frame = pl.DataFrame(
            {
                "symbol": ["2330"] * bars_available,
                "trading_date": [
                    __import__("datetime").date(2026, 1, 1)
                    + __import__("datetime").timedelta(days=i)
                    for i in range(bars_available)
                ],
                "open": bars["close"][:bars_available],
                "high": bars["high"][:bars_available],
                "low": bars["low"][:bars_available],
                "close": bars["close"][:bars_available],
                "volume": bars["volume"][:bars_available],
                "turnover": bars["close"][:bars_available] * bars["volume"][:bars_available],
                "benchmark_close": bars["close"][:bars_available] * 100,
                "shares_outstanding": [1e9] * bars_available,
            }
        )
        built = panel_mod.build(frame, price_basis=panel_mod.PriceBasis.RAW)

        for name, spec in sorted(registry.REGISTRY.items()):
            values = spec.compute(built.frame)
            assert values.len() == bars_available, (
                f"{name} returned {values.len()} values for {bars_available} bars; "
                f"an indicator must produce one value per bar, null where undefined"
            )
            if bars_available < spec.lookback:
                # No exemptions. An indicator that emits a number before its
                # window is full has substituted a shorter window, which
                # `QUANT_ENGINE.md` §1.3 forbids precisely because the result is
                # indistinguishable from a real one.
                assert values.drop_nulls().len() == 0, (
                    f"{name} produced a value from {bars_available} bars when it "
                    f"declares a lookback of {spec.lookback}"
                )
