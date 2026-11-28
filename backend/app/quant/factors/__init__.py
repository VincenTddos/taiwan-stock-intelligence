"""Factors: declaration, computation, processing, validation.

`compute` is the only place raw factor values are produced, and it takes a frame
rather than a session on purpose — the look-ahead injection test poisons a frame
and recomputes, which is only possible while nothing here can reach past its
argument.
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from app.quant.factors.library import REGISTRY, build_registry
from app.quant.factors.pipeline import process, process_cross_section
from app.quant.factors.spec import (
    FactorCategory,
    FactorRegistry,
    FactorSpec,
    Neutralisation,
    Standardisation,
)
from app.quant.factors.validation import (
    FactorValidity,
    Verdict,
    apply_thresholds,
    attach_correlations,
    correlation_matrix,
    evaluate,
    forward_returns,
)


def compute_raw(
    frame: pl.DataFrame,
    specs: Sequence[FactorSpec],
    *,
    symbol_column: str = "symbol",
) -> dict[str, pl.DataFrame]:
    """Raw values per factor, one frame each with symbol, date and raw_value.

    Computed per symbol so that a rolling window inside a factor cannot span two
    companies — the same rule the indicator layer follows, for the same reason.
    """
    out: dict[str, pl.DataFrame] = {}
    for spec in specs:
        parts: list[pl.DataFrame] = []
        for (_symbol,), group in frame.group_by([symbol_column], maintain_order=True):
            values = spec.compute(group).cast(pl.Float64)
            parts.append(
                pl.DataFrame(
                    {
                        "symbol": group[symbol_column],
                        "trading_date": group["trading_date"],
                        "raw_value": values.rename("raw_value"),
                    }
                )
            )
        out[spec.name] = pl.concat(parts, how="vertical") if parts else pl.DataFrame()
    return out


__all__ = [
    "REGISTRY",
    "FactorCategory",
    "FactorRegistry",
    "FactorSpec",
    "FactorValidity",
    "Neutralisation",
    "Standardisation",
    "Verdict",
    "apply_thresholds",
    "attach_correlations",
    "build_registry",
    "compute_raw",
    "correlation_matrix",
    "evaluate",
    "forward_returns",
    "process",
    "process_cross_section",
]
