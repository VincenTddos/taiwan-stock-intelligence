"""Look-ahead injection: the most important test in this repository.

The method is the one in `QUANT_ENGINE.md` §10.2. Take a panel, replace
everything after a cutoff with absurd values, recompute, and assert that not one
number on or before the cutoff moved. Any function that reads forward — a
centred window, a fill that propagates backwards, a mean taken over the whole
series — changes something, and the test names it.

It is parameterised over *every* registered indicator and *every* computable
factor rather than over a chosen few. The value of the property is that it holds
universally; checking it for the factors someone remembered to list is how a
leak survives.

Two things this specifically catches, both of which produce entirely plausible
backtests:

* a forward return joined with a frame-wide shift, so the last rows of one
  symbol take the first rows of the next
* a cross-sectional statistic computed over the whole panel instead of per date,
  which lets December inform January's z-score
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.quant import factors as fct
from app.quant import indicators as ind
from app.quant import panel as panel_mod

CUTOFF = date(2025, 3, 3)
POISON = 1_000.0


@pytest.fixture(scope="module")
def clean_panel() -> panel_mod.Panel:
    """Sixty symbols over two years, enough for a 250-session window."""
    rng = np.random.default_rng(4242)
    symbols = [f"{2000 + i}" for i in range(60)]
    days = 700
    start = date(2024, 1, 1)
    dates = [start + timedelta(days=i) for i in range(days)]
    sectors = ["semiconductor", "finance", "plastics", "shipping"]
    benchmark = 10_000 * np.exp(np.cumsum(rng.normal(0, 0.008, days)))

    rows: list[dict[str, object]] = []
    for index, symbol in enumerate(symbols):
        close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.02, days)))
        volume = rng.integers(1_000, 5_000_000, days).astype(float)
        for i, day in enumerate(dates):
            rows.append(
                {
                    "symbol": symbol,
                    "trading_date": day,
                    "open": float(close[i]),
                    "high": float(close[i] * 1.012),
                    "low": float(close[i] * 0.988),
                    "close": float(close[i]),
                    "volume": float(volume[i]),
                    "turnover": float(close[i] * volume[i]),
                    "benchmark_close": float(benchmark[i]),
                    "shares_outstanding": float(1e8 * (index + 1)),
                    "sector": sectors[index % len(sectors)],
                }
            )
    return panel_mod.build(
        pl.DataFrame(rows), price_basis=panel_mod.PriceBasis.RAW, benchmark="TAIEX"
    )


@pytest.fixture(scope="module")
def poisoned_panel(clean_panel: panel_mod.Panel) -> panel_mod.Panel:
    """The same panel with every value after the cutoff made absurd."""
    poisoned = clean_panel.frame.with_columns(
        [
            pl.when(pl.col("trading_date") > CUTOFF)
            .then(pl.col(column) * POISON)
            .otherwise(pl.col(column))
            .alias(column)
            for column in ("open", "high", "low", "close", "volume", "turnover", "benchmark_close")
        ]
    )
    return panel_mod.build(poisoned, price_basis=panel_mod.PriceBasis.RAW, benchmark="TAIEX")


def _before_cutoff(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.filter(pl.col("trading_date") <= CUTOFF).sort(["symbol", "trading_date"])


@pytest.fixture(scope="module")
def clean_indicators(clean_panel: panel_mod.Panel) -> pl.DataFrame:
    return _before_cutoff(ind.compute(clean_panel))


@pytest.fixture(scope="module")
def poisoned_indicators(poisoned_panel: panel_mod.Panel) -> pl.DataFrame:
    return _before_cutoff(ind.compute(poisoned_panel))


@pytest.mark.parametrize("name", sorted(ind.REGISTRY))
def test_no_indicator_reads_the_future(name, clean_indicators, poisoned_indicators):
    """Every indicator, one test each.

    A failure here does not mean the indicator is inaccurate. It means the
    indicator's value today depends on tomorrow's price, which makes every
    backtest that used it worthless in a way no accuracy check would reveal.
    """
    if name not in clean_indicators.columns:
        pytest.skip(f"{name} was not computable on the test panel")

    clean = clean_indicators[name].to_numpy().astype(float)
    poisoned = poisoned_indicators[name].to_numpy().astype(float)
    np.testing.assert_array_equal(
        np.isnan(clean), np.isnan(poisoned), err_msg=f"{name} changed which values exist"
    )
    np.testing.assert_allclose(
        clean[~np.isnan(clean)],
        poisoned[~np.isnan(poisoned)],
        rtol=0,
        atol=0,
        err_msg=f"{name} changed after the future was poisoned — it reads forward",
    )


def _factor_scores(price_panel: panel_mod.Panel) -> dict[str, pl.DataFrame]:
    indicator_frame = ind.compute(price_panel)
    joined = price_panel.frame.join(indicator_frame, on=["symbol", "trading_date"], how="left")
    specs = fct.REGISTRY.computable(joined.columns)
    raw = fct.compute_raw(joined, specs)

    controls = joined.select("symbol", "trading_date", "sector")
    if "log_market_cap" in raw:
        controls = controls.join(
            raw["log_market_cap"].rename({"raw_value": "log_market_cap"}),
            on=["symbol", "trading_date"],
            how="left",
        )
    return {
        spec.name: _before_cutoff(
            fct.process(
                raw[spec.name].join(controls, on=["symbol", "trading_date"], how="left"), spec
            )
        )
        for spec in specs
    }


@pytest.fixture(scope="module")
def clean_factors(clean_panel: panel_mod.Panel) -> dict[str, pl.DataFrame]:
    return _factor_scores(clean_panel)


@pytest.fixture(scope="module")
def poisoned_factors(poisoned_panel: panel_mod.Panel) -> dict[str, pl.DataFrame]:
    return _factor_scores(poisoned_panel)


def _computable_factor_names() -> list[str]:
    """The factors the synthetic panel can produce, resolved at collection time."""
    columns = [
        "symbol",
        "trading_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "benchmark_close",
        "shares_outstanding",
        "sector",
        *ind.REGISTRY,
    ]
    return sorted(spec.name for spec in fct.REGISTRY.computable(columns))


@pytest.mark.parametrize("name", _computable_factor_names())
@pytest.mark.parametrize("column", ["raw_value", "score"])
def test_no_factor_reads_the_future(name, column, clean_factors, poisoned_factors):
    clean = clean_factors[name][column].to_numpy().astype(float)
    poisoned = poisoned_factors[name][column].to_numpy().astype(float)
    np.testing.assert_array_equal(
        np.isnan(clean),
        np.isnan(poisoned),
        err_msg=f"{name}.{column} changed which values exist",
    )
    np.testing.assert_allclose(
        clean[~np.isnan(clean)],
        poisoned[~np.isnan(poisoned)],
        rtol=0,
        atol=0,
        err_msg=f"{name}.{column} changed after poisoning — it reads forward",
    )


class TestTheTestItself:
    """A test that cannot fail is worse than no test, so make it fail."""

    def test_a_deliberately_leaking_indicator_is_caught(self, clean_panel, poisoned_panel):
        """A centred window is the classic accidental leak. It must be detected."""

        def leaking(frame: pl.DataFrame) -> pl.Series:
            # Tomorrow's close, which no one has on the day.
            return frame["close"].shift(-1)

        clean = _before_cutoff(
            pl.DataFrame(
                {
                    "symbol": clean_panel.frame["symbol"],
                    "trading_date": clean_panel.frame["trading_date"],
                    "leak": pl.concat(
                        [
                            leaking(group)
                            for _key, group in clean_panel.frame.group_by(
                                ["symbol"], maintain_order=True
                            )
                        ]
                    ),
                }
            )
        )
        poisoned = _before_cutoff(
            pl.DataFrame(
                {
                    "symbol": poisoned_panel.frame["symbol"],
                    "trading_date": poisoned_panel.frame["trading_date"],
                    "leak": pl.concat(
                        [
                            leaking(group)
                            for _key, group in poisoned_panel.frame.group_by(
                                ["symbol"], maintain_order=True
                            )
                        ]
                    ),
                }
            )
        )
        assert not np.allclose(
            clean["leak"].to_numpy().astype(float),
            poisoned["leak"].to_numpy().astype(float),
            equal_nan=True,
        ), "the injection method failed to notice a one-day look-ahead"


class TestForwardReturnAlignment:
    def test_the_forward_return_never_crosses_a_symbol_boundary(self, clean_panel):
        """The subtlest leak in the file, and the easiest to write by accident.

        A frame-wide `shift(-20)` hands the last twenty rows of one symbol the
        first twenty of the next. Nothing errors; the IC simply improves.
        """
        returns = fct.forward_returns(clean_panel.frame, horizon=20)
        joined = returns.join(
            clean_panel.frame.select("symbol", "trading_date", "close"),
            on=["symbol", "trading_date"],
        )
        for _key, group in joined.sort(["symbol", "trading_date"]).group_by(
            ["symbol"], maintain_order=True
        ):
            values = group["forward_return"].to_list()
            # The final twenty sessions of every symbol have no future to
            # measure, and must be null rather than borrowed.
            assert all(v is None for v in values[-20:]), (
                "a forward return exists past the end of a symbol's history"
            )
            closes = group["close"].to_list()
            assert values[0] == pytest.approx(closes[20] / closes[0] - 1.0)

    def test_truncating_the_panel_removes_everything_after_the_cutoff(self, clean_panel):
        truncated = clean_panel.truncated(CUTOFF)
        assert truncated.frame["trading_date"].max() <= CUTOFF
        assert truncated.frame.height < clean_panel.frame.height
