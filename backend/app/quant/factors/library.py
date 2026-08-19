"""The factor library.

Every factor named in `QUANT_ENGINE.md` §2.2 is declared here, including the
ones whose inputs this platform does not yet ingest. Declaring them is not
optimism: a spec with `inputs=("eps_ttm",)` and no `eps_ttm` column produces an
explicit "not computable — missing eps_ttm", which is a statement about the data
pipeline. Leaving them out would leave nothing to notice.

As of today the panel carries prices, volume, turnover, the index and the
security master, so momentum, volatility, liquidity and size compute; value,
growth and quality wait on financial statements (Phase 2's monthly revenue and
statement ingestion, not yet written), and sentiment, news and AI exposure wait
on Phases 4, 5 and 7.

Two conventions worth stating once:

**`direction` is applied last, in the pipeline, never here.** A raw factor is
the quantity as it is normally quoted — `hv_60` is volatility, not negative
volatility — so a stored raw value can be read against a published one. The sign
flip that makes "higher is better" true belongs to the score, not the
measurement.

**Momentum skips the most recent month.** `momentum_12_1` is the return from
`t-252` to `t-21`, not to `t`. The last month reverses, and including it mixes a
positive-premium effect with a negative-premium one — the classic way to build a
factor that tests as noise.
"""

from __future__ import annotations

import polars as pl

from app.quant.factors.spec import (
    FactorCategory as C,
)
from app.quant.factors.spec import (
    FactorRegistry,
    FactorSpec,
    Neutralisation,
    Standardisation,
)


def _ratio(frame: pl.DataFrame, near: int, far: int) -> pl.Series:
    """`close[t-near] / close[t-far] - 1`, the skipped-window momentum shape."""
    close = frame["close"].cast(pl.Float64)
    a, b = close.shift(near), close.shift(far)
    return pl.select(
        pl.when(b.is_null() | (b == 0) | a.is_null()).then(None).otherwise(a / b - 1.0).alias("v")
    ).to_series()


def _passthrough(column: str) -> object:
    def compute(frame: pl.DataFrame) -> pl.Series:
        return frame[column].cast(pl.Float64)

    return compute


def _unavailable(column: str) -> object:
    """A factor whose inputs are not ingested yet.

    It still needs a callable so the spec is complete; reaching it means the
    availability check was bypassed, and that should be loud.
    """

    def compute(frame: pl.DataFrame) -> pl.Series:
        raise RuntimeError(
            f"factor input '{column}' is not present; this factor should have been "
            f"reported as not computable rather than computed"
        )

    return compute


def build_registry() -> FactorRegistry:
    registry = FactorRegistry()

    def add(
        name: str,
        category: C,
        description: str,
        formula: str,
        inputs: tuple[str, ...],
        compute: object,
        lookback_days: int,
        min_periods: int,
        direction: int = 1,
        standardize: Standardisation = Standardisation.ZSCORE,
        neutralize: tuple[Neutralisation, ...] = (Neutralisation.SECTOR,),
        winsorize: tuple[float, float] | None = (0.01, 0.99),
    ) -> None:
        registry.add(
            FactorSpec(
                name=name,
                category=category,
                description=description,
                formula=formula,
                inputs=inputs,
                compute=compute,  # type: ignore[arg-type]
                lookback_days=lookback_days,
                min_periods=min_periods,
                direction=direction,
                standardize=standardize,
                neutralize=neutralize,
                winsorize=winsorize,
            )
        )

    # ------------------------------------------------------------- momentum
    add(
        "momentum_12_1",
        C.MOMENTUM,
        "Twelve-month return skipping the most recent month",
        "close[t-21] / close[t-252] - 1",
        ("close",),
        lambda f: _ratio(f, 21, 252),
        lookback_days=252,
        min_periods=200,
        neutralize=(Neutralisation.SECTOR, Neutralisation.SIZE),
    )
    add(
        "momentum_6_1",
        C.MOMENTUM,
        "Six-month return skipping the most recent month",
        "close[t-21] / close[t-126] - 1",
        ("close",),
        lambda f: _ratio(f, 21, 126),
        lookback_days=126,
        min_periods=100,
        neutralize=(Neutralisation.SECTOR, Neutralisation.SIZE),
    )
    add(
        "momentum_1m",
        C.MOMENTUM,
        "One-month return; short-horizon reversal, so higher is worse",
        "close[t] / close[t-21] - 1",
        ("close",),
        lambda f: _ratio(f, 0, 21),
        lookback_days=21,
        min_periods=18,
        direction=-1,
    )
    add(
        "rs_vs_taiex_60",
        C.MOMENTUM,
        "Sixty-day return minus the index's",
        "roc_60(stock) - roc_60(index)",
        ("relative_strength_taiex_60",),
        _passthrough("relative_strength_taiex_60"),
        lookback_days=60,
        min_periods=55,
    )
    add(
        "high_proximity_250",
        C.MOMENTUM,
        "Close relative to the 52-week high",
        "close / max(high, 250)",
        ("high_proximity_250",),
        _passthrough("high_proximity_250"),
        lookback_days=250,
        min_periods=200,
    )
    add(
        "momentum_consistency_60",
        C.MOMENTUM,
        "Fraction of the last sixty sessions that closed up",
        "mean(close_t > close_{t-1}, 60)",
        ("momentum_consistency_60",),
        _passthrough("momentum_consistency_60"),
        lookback_days=61,
        min_periods=55,
    )

    # ----------------------------------------------------------- volatility
    for name, source, look in (
        ("hv_60", "hv_60", 61),
        ("idio_vol_60", "idio_vol_60", 61),
        ("beta_250", "beta_250", 251),
        ("downside_deviation_60", "downside_deviation_60", 61),
        ("max_drawdown_250", "max_drawdown_250", 499),
    ):
        add(
            name,
            C.VOLATILITY,
            f"{name.replace('_', ' ')} — the low-volatility anomaly, so higher is worse",
            source,
            (source,),
            _passthrough(source),
            lookback_days=look,
            min_periods=int(look * 0.8),
            direction=-1,
        )

    # ------------------------------------------------------------ liquidity
    add(
        "avg_turnover_20d",
        C.LIQUIDITY,
        "Average daily traded value over twenty sessions",
        "mean(turnover, 20)",
        ("turnover",),
        lambda f: f["turnover"].cast(pl.Float64).rolling_mean(window_size=20),
        lookback_days=20,
        min_periods=15,
    )
    add(
        "turnover_rate_20d",
        C.LIQUIDITY,
        "Average share turnover relative to shares in issue",
        "mean(volume / shares_outstanding, 20)",
        ("turnover_rate",),
        lambda f: f["turnover_rate"].cast(pl.Float64).rolling_mean(window_size=20),
        lookback_days=20,
        min_periods=15,
    )
    add(
        "amihud_illiquidity_60",
        C.LIQUIDITY,
        "Price impact per unit of turnover; illiquidity, so higher is worse",
        "mean(|return| / turnover, 60) * 1e9",
        ("amihud_illiquidity_20",),
        lambda f: f["amihud_illiquidity_20"].cast(pl.Float64).rolling_mean(window_size=60),
        lookback_days=80,
        min_periods=60,
        direction=-1,
    )
    add(
        "zero_volume_days_60",
        C.LIQUIDITY,
        "Fraction of sessions with no trading at all; higher is worse",
        "mean(volume == 0, 60)",
        ("zero_volume_days_60",),
        _passthrough("zero_volume_days_60"),
        lookback_days=60,
        min_periods=50,
        direction=-1,
    )

    # ----------------------------------------------------------------- size
    add(
        "log_market_cap",
        C.SIZE,
        "Log market capitalisation; the small-cap effect, so higher is worse",
        "log(close * shares_outstanding)",
        ("close", "shares_outstanding"),
        lambda f: pl.select(
            pl.when(
                f["shares_outstanding"].cast(pl.Float64).is_null()
                | (f["shares_outstanding"].cast(pl.Float64) <= 0)
                | (f["close"].cast(pl.Float64) <= 0)
            )
            .then(None)
            .otherwise(
                (f["close"].cast(pl.Float64) * f["shares_outstanding"].cast(pl.Float64)).log()
            )
            .alias("v")
        ).to_series(),
        lookback_days=1,
        min_periods=1,
        direction=-1,
        # Neutralising size against size would leave nothing. This is the one
        # factor the others are neutralised *against*.
        neutralize=(),
    )
    add(
        "float_market_cap",
        C.SIZE,
        "Free-float market capitalisation",
        "log(close * free_float_shares)",
        ("free_float_shares",),
        _unavailable("free_float_shares"),
        lookback_days=1,
        min_periods=1,
        direction=-1,
        neutralize=(),
    )

    # ---------------------------------------------------------------- value
    for name, column, formula, description in (
        ("earnings_yield", "eps_ttm", "eps_ttm / close", "Trailing earnings over price"),
        ("book_to_price", "book_value_per_share", "bvps / close", "Book value over price"),
        ("sales_to_price", "revenue_per_share_ttm", "rps_ttm / close", "Sales over price"),
        ("fcf_yield", "fcf_per_share_ttm", "fcfps_ttm / close", "Free cash flow over price"),
        ("dividend_yield", "cash_dividend_ttm", "dividend_ttm / close", "Cash dividend over price"),
        ("ev_to_ebitda_inv", "ev_to_ebitda", "1 / (EV/EBITDA)", "Inverted enterprise multiple"),
    ):
        add(
            name,
            C.VALUE,
            description,
            formula,
            (column,),
            _unavailable(column),
            lookback_days=252,
            min_periods=1,
        )

    # --------------------------------------------------------------- growth
    for name, column, description in (
        ("revenue_yoy", "revenue_yoy", "Latest monthly revenue, year on year"),
        ("revenue_yoy_3m", "revenue_yoy_3m", "Three-month revenue, year on year"),
        ("revenue_acceleration", "revenue_yoy", "Change in the year-on-year revenue growth rate"),
        ("eps_yoy", "eps_yoy", "Latest quarterly EPS, year on year"),
        ("eps_growth_4q", "eps_ttm", "Trailing four quarters of EPS against the prior four"),
        ("operating_income_yoy", "operating_income_yoy", "Operating income, year on year"),
        (
            "revenue_surprise",
            "revenue_monthly",
            "Monthly revenue against its own twelve-month trend",
        ),
    ):
        add(
            name,
            C.GROWTH,
            description,
            column,
            (column,),
            _unavailable(column),
            lookback_days=400,
            min_periods=1,
        )

    # -------------------------------------------------------------- quality
    for name, column, direction, description in (
        ("roe", "roe_ttm", 1, "Return on equity, trailing twelve months"),
        ("roa", "roa_ttm", 1, "Return on assets, trailing twelve months"),
        ("roic", "roic_ttm", 1, "Return on invested capital"),
        ("gross_margin", "gross_margin_ttm", 1, "Gross margin"),
        ("operating_margin", "operating_margin_ttm", 1, "Operating margin"),
        (
            "margin_stability",
            "gross_margin_ttm",
            -1,
            "Volatility of gross margin over eight quarters",
        ),
        ("accruals", "accruals", -1, "Net income less operating cash flow, over assets"),
        ("debt_to_equity", "debt_to_equity", -1, "Leverage"),
        ("interest_coverage", "interest_coverage", 1, "Operating income over interest expense"),
        ("cash_conversion", "cash_conversion", 1, "Operating cash flow over net income"),
        (
            "earnings_variability",
            "eps_ttm",
            -1,
            "Coefficient of variation of EPS over twelve quarters",
        ),
    ):
        add(
            name,
            C.QUALITY,
            description,
            column,
            (column,),
            _unavailable(column),
            lookback_days=1000,
            min_periods=1,
            direction=direction,
        )

    # ------------------------------------------------- sentiment / news / AI
    for name, column, category, description in (
        ("news_sentiment_7d", "news_sentiment_7d", C.SENTIMENT, "Credibility-weighted sentiment"),
        (
            "news_sentiment_change",
            "news_sentiment_7d",
            C.SENTIMENT,
            "Sentiment against the prior week",
        ),
        (
            "sentiment_dispersion",
            "news_sentiment_std_7d",
            C.SENTIMENT,
            "Disagreement across sources",
        ),
        ("news_count_ratio_7d", "news_count_7d", C.NEWS, "News volume against its own baseline"),
        (
            "news_importance_sum_7d",
            "news_importance_7d",
            C.NEWS,
            "Weighted count of important stories",
        ),
        ("news_novelty_7d", "news_novelty_7d", C.NEWS, "Distance from the last month of coverage"),
        (
            "event_intensity_7d",
            "event_intensity_7d",
            C.NEWS,
            "Weighted count of high-impact events",
        ),
        (
            "ai_supply_chain_centrality",
            "ai_centrality",
            C.AI_EXPOSURE,
            "Weighted degree in the AI graph",
        ),
        ("ai_theme_strength", "ai_theme_strength", C.AI_EXPOSURE, "Edge strength to the AI theme"),
        (
            "ai_revenue_share",
            "ai_revenue_share",
            C.AI_EXPOSURE,
            "Disclosed or inferred AI revenue share",
        ),
        (
            "ai_news_share_60d",
            "ai_news_share_60d",
            C.AI_EXPOSURE,
            "Share of coverage mentioning AI",
        ),
        ("ai_beta", "ai_basket_close", C.AI_EXPOSURE, "Beta to an equal-weighted AI basket"),
    ):
        add(
            name,
            category,
            description,
            column,
            (column,),
            _unavailable(column),
            lookback_days=250,
            min_periods=1,
        )

    return registry


REGISTRY = build_registry()

__all__ = ["REGISTRY", "build_registry"]
