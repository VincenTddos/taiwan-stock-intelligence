"""Raw factor value → comparable score.

The order is fixed and the order matters:

    winsorize → neutralise (sector, then size) → standardise → direction

Winsorising first stops one broken print from setting the mean that everything
else is measured against. Neutralising before standardising means the z-score is
of the *residual*, so a factor that is really a sector bet stops looking like a
factor. Direction is applied last so the stored raw and z-score are the quantity
as normally quoted, and only the final score carries "higher is better".

Every step is cross-sectional — across symbols on one date, never along time for
one symbol. That is what makes the whole pipeline immune to look-ahead by
construction: a value on date `t` is a function of values on date `t` only.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from app.quant.factors.spec import FactorSpec, Neutralisation, Standardisation

#: Below this many observations a cross-section is not a cross-section. A
#: z-score over four stocks is arithmetic, not a comparison, and a percentile
#: over four stocks takes four distinct values.
MIN_CROSS_SECTION = 20


def winsorize(values: pl.Series, lower: float, upper: float) -> pl.Series:
    """Clip to quantiles. Clipped, not dropped — an extreme is still a ranking."""
    if values.drop_nulls().len() == 0:
        return values
    low = values.quantile(lower, interpolation="linear")
    high = values.quantile(upper, interpolation="linear")
    if low is None or high is None:
        return values
    return values.clip(low, high)


def _demedian_by_group(values: pl.Series, groups: pl.Series) -> pl.Series:
    """Subtract each group's median. Median, not mean: sectors are small."""
    # A window expression, not group_by + join: polars does not promise a join
    # keeps the left frame's row order (`maintain_order` defaults to "none"),
    # and these values are matched back to symbols purely by position.
    frame = pl.DataFrame({"v": values, "g": groups.fill_null("__none__")})
    return frame.select(pl.col("v") - pl.col("v").median().over("g"))["v"].rename(
        values.name or "v"
    )


def _residualise(values: pl.Series, control: pl.Series) -> pl.Series:
    """OLS residual of `values` on `control`, with an intercept.

    Falls back to the input unchanged when the control has no variation — a
    universe where every stock is the same size cannot be size-neutralised, and
    returning zeros there would destroy the factor rather than neutralise it.
    """
    y = values.to_numpy().astype(float)
    x = control.to_numpy().astype(float)
    mask = ~(np.isnan(y) | np.isnan(x))
    if mask.sum() < 3 or np.nanstd(x[mask]) == 0:
        return values

    design = np.column_stack([np.ones(mask.sum()), x[mask]])
    coefficients, *_ = np.linalg.lstsq(design, y[mask], rcond=None)
    residual = np.full_like(y, np.nan)
    residual[mask] = y[mask] - design @ coefficients
    return pl.Series(values.name or "v", residual)


def standardise(values: pl.Series, method: Standardisation, sectors: pl.Series | None) -> pl.Series:
    if method is Standardisation.RANK:
        # Percentile in [0, 1]. `rank` over nulls yields null, which is what we
        # want: an absent factor must not become a mid-ranked one.
        ranks = values.rank(method="average")
        count = values.drop_nulls().len()
        return (ranks - 1) / max(count - 1, 1)

    if method is Standardisation.SECTOR_ZSCORE and sectors is not None:
        # Window expressions keep row order by construction; see _demedian_by_group.
        frame = pl.DataFrame({"v": values, "g": sectors.fill_null("__none__")})
        group_mean = pl.col("v").mean().over("g")
        group_sd = pl.col("v").std(ddof=1).over("g")
        return frame.select(
            pl.when(group_sd.is_null() | (group_sd == 0))
            .then(None)
            .otherwise((pl.col("v") - group_mean) / group_sd)
            .alias("v")
        ).to_series()

    mean = values.mean()
    sd = values.std(ddof=1)
    if mean is None or sd is None or sd == 0:
        return pl.Series(values.name or "v", [None] * values.len(), dtype=pl.Float64)
    return (values - mean) / sd


def process_cross_section(
    raw: pl.Series,
    spec: FactorSpec,
    *,
    sectors: pl.Series | None = None,
    size: pl.Series | None = None,
) -> pl.DataFrame:
    """One date's raw values → raw, zscore, percentile, sector_zscore, score.

    All five are returned rather than only the final score. The raw value is
    what can be checked against a published number; the z-score is what a
    composite consumes; the percentile is what a human reads; and the
    sector z-score answers "good for a bank" rather than "good".
    """
    values = raw.cast(pl.Float64)
    observed = values.drop_nulls().len()

    if observed < MIN_CROSS_SECTION:
        nulls = pl.Series([None] * values.len(), dtype=pl.Float64)
        return pl.DataFrame(
            {
                "raw_value": values,
                "zscore": nulls,
                "percentile": nulls,
                "sector_zscore": nulls,
                "score": nulls,
            }
        )

    working = values
    if spec.winsorize:
        working = winsorize(working, *spec.winsorize)

    if Neutralisation.SECTOR in spec.neutralize and sectors is not None:
        working = _demedian_by_group(working, sectors)
    if Neutralisation.SIZE in spec.neutralize and size is not None:
        working = _residualise(working, size)

    zscore = standardise(working, Standardisation.ZSCORE, sectors)
    percentile = standardise(working, Standardisation.RANK, sectors)
    sector_zscore = (
        standardise(working, Standardisation.SECTOR_ZSCORE, sectors)
        if sectors is not None
        else pl.Series([None] * values.len(), dtype=pl.Float64)
    )
    primary = {
        Standardisation.ZSCORE: zscore,
        Standardisation.RANK: percentile,
        Standardisation.SECTOR_ZSCORE: sector_zscore,
    }[spec.standardize]

    return pl.DataFrame(
        {
            "raw_value": values,
            "zscore": zscore,
            "percentile": percentile,
            "sector_zscore": sector_zscore,
            "score": primary * float(spec.direction),
        }
    )


def process(
    frame: pl.DataFrame,
    spec: FactorSpec,
    *,
    raw_column: str = "raw_value",
    date_column: str = "trading_date",
) -> pl.DataFrame:
    """Run `process_cross_section` for every date in the frame."""
    parts: list[pl.DataFrame] = []
    for (_day,), group in frame.group_by([date_column], maintain_order=True):
        processed = process_cross_section(
            group[raw_column],
            spec,
            sectors=group["sector"] if "sector" in group.columns else None,
            size=group["log_market_cap"] if "log_market_cap" in group.columns else None,
        )
        parts.append(
            pl.concat(
                [group.select("symbol", date_column), processed.drop("raw_value")], how="horizontal"
            ).with_columns(processed["raw_value"].alias("raw_value"))
        )
    return pl.concat(parts, how="vertical") if parts else pl.DataFrame()


__all__ = [
    "MIN_CROSS_SECTION",
    "process",
    "process_cross_section",
    "standardise",
    "winsorize",
]
