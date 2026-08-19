"""Factor validity: the report that decides what may enter a score.

Eight checks, from `QUANT_ENGINE.md` §2.4. Seven measure whether a factor
predicts anything; the eighth asks whether it is merely another factor wearing a
different name.

Two properties of this module are deliberate.

**The forward return is aligned by construction, not by hope.** The IC on date
`t` pairs the factor known at `t` with the return from `t` to `t + horizon` —
computed by shifting *within each symbol* after sorting, so a symbol with a
shorter history cannot borrow the next symbol's future. Getting this wrong
inflates IC in the most convincing possible way.

**An insufficient sample is reported as insufficient, never as a result.** A
factor measured over eleven dates has an IC; it does not have evidence. Every
verdict carries the number of observations behind it, and the verdict for a thin
sample is `INSUFFICIENT_DATA` rather than a number that would be quoted later
without its sample size attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np
import numpy.typing as npt
import polars as pl

#: Minimum cross-section for one date's IC to mean anything.
MIN_SYMBOLS_PER_DATE = 20
#: Minimum number of dated ICs before their mean is reportable.
MIN_DATES = 60

THRESHOLDS: dict[str, float] = {
    "abs_ic": 0.02,
    "abs_rank_ic": 0.02,
    "ic_ir": 0.3,
    "spread_t_stat": 2.0,
    "coverage": 0.70,
    "stability": 0.50,
    "max_correlation": 0.80,
}


class Verdict(StrEnum):
    PASS = "PASS"  # noqa: S105 - a verdict, not a credential
    RESEARCH_ONLY = "RESEARCH_ONLY"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_COMPUTABLE = "NOT_COMPUTABLE"


@dataclass(slots=True)
class FactorValidity:
    """One factor's report card."""

    factor: str
    verdict: Verdict
    dates: int = 0
    mean_symbols: float = 0.0
    ic: float | None = None
    rank_ic: float | None = None
    ic_ir: float | None = None
    ic_t_stat: float | None = None
    quantile_returns: list[float] = field(default_factory=list)
    monotonic: bool | None = None
    spread_return: float | None = None
    spread_t_stat: float | None = None
    coverage: float | None = None
    stability: float | None = None
    max_correlation: float | None = None
    correlated_with: str | None = None
    failures: list[str] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "verdict": str(self.verdict),
            "dates": self.dates,
            "mean_symbols": round(self.mean_symbols, 2),
            "ic": _round(self.ic),
            "rank_ic": _round(self.rank_ic),
            "ic_ir": _round(self.ic_ir),
            "ic_t_stat": _round(self.ic_t_stat),
            "quantile_returns": [round(q, 6) for q in self.quantile_returns],
            "monotonic": self.monotonic,
            "spread_return": _round(self.spread_return),
            "spread_t_stat": _round(self.spread_t_stat),
            "coverage": _round(self.coverage),
            "stability": _round(self.stability),
            "max_correlation": _round(self.max_correlation),
            "correlated_with": self.correlated_with,
            "failures": self.failures,
            "missing_inputs": self.missing_inputs,
        }


def _round(value: float | None, places: int = 6) -> float | None:
    return None if value is None else round(float(value), places)


def forward_returns(panel: pl.DataFrame, horizon: int = 20) -> pl.DataFrame:
    """Return from each date to `horizon` sessions later, per symbol.

    Shifted within symbol after sorting. A frame-wide shift would hand the last
    rows of one symbol the first rows of the next — which does not look like a
    bug, it looks like a factor that works.
    """
    return (
        panel.sort(["symbol", "trading_date"])
        .with_columns(
            (pl.col("close").shift(-horizon).over("symbol") / pl.col("close") - 1.0).alias(
                "forward_return"
            )
        )
        .select("symbol", "trading_date", "forward_return")
    )


def _pearson(x: npt.NDArray[np.float64], y: npt.NDArray[np.float64]) -> float | None:
    """Correlation over the pairs where both sides exist.

    Pairwise deletion, not `corrcoef` on the raw arrays — that propagates a
    single NaN into the answer and returns NaN for an otherwise perfectly good
    sample. Dropping the pair rather than the value keeps both series measured
    over the same observations, which is what makes the covariance meaningful.
    """
    both = ~(np.isnan(x) | np.isnan(y))
    if both.sum() < 3:
        return None
    xs, ys = x[both], y[both]
    if xs.std() == 0 or ys.std() == 0:
        return None
    return float(np.corrcoef(xs, ys)[0, 1])


def _spearman(x: npt.NDArray[np.float64], y: npt.NDArray[np.float64]) -> float | None:
    """Rank correlation, computed from ranks rather than pulled from scipy.

    Ties get their average rank, which is what makes this Spearman rather than
    an approximation of it — factor values tie constantly once a factor is a
    count or a flag.
    """
    if x.size < 3:
        return None
    return _pearson(_ranks(x), _ranks(y))


def _ranks(values: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    order = values.argsort()
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(values.size, dtype=float)
    # Average the ranks of tied values.
    unique, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
    if counts.max() > 1:
        sums = np.zeros(unique.size)
        np.add.at(sums, inverse, ranks)
        ranks = (sums / counts)[inverse]
    return ranks


def _column_mean(values: npt.NDArray[np.float64]) -> float:
    """Mean ignoring NaN, or NaN when every value is NaN — without a warning."""
    finite = values[~np.isnan(values)]
    return float(finite.mean()) if finite.size else float("nan")


def _t_stat(series: npt.NDArray[np.float64]) -> float | None:
    clean = series[~np.isnan(series)]
    if clean.size < 3 or clean.std(ddof=1) == 0:
        return None
    return float(clean.mean() / (clean.std(ddof=1) / np.sqrt(clean.size)))


def evaluate(
    scores: pl.DataFrame,
    returns: pl.DataFrame,
    *,
    factor: str,
    universe_size: int | None = None,
    quantiles: int = 5,
) -> FactorValidity:
    """Score one factor against forward returns.

    `scores` carries symbol, trading_date, score (direction applied) and
    raw_value; `returns` carries symbol, trading_date, forward_return.
    """
    report = FactorValidity(factor=factor, verdict=Verdict.INSUFFICIENT_DATA)

    joined = (
        scores.join(returns, on=["symbol", "trading_date"], how="inner")
        .drop_nulls(["score", "forward_return"])
        .sort(["trading_date", "symbol"])
    )
    if joined.is_empty():
        report.failures.append("no overlapping factor and forward-return observations")
        return report

    ics: list[float] = []
    rank_ics: list[float] = []
    quantile_rows: list[list[float]] = []
    symbol_counts: list[int] = []

    for (_day,), group in joined.group_by(["trading_date"], maintain_order=True):
        if group.height < MIN_SYMBOLS_PER_DATE:
            continue
        x = group["score"].to_numpy().astype(float)
        y = group["forward_return"].to_numpy().astype(float)
        symbol_counts.append(group.height)

        pearson = _pearson(x, y)
        spearman = _spearman(x, y)
        if pearson is not None:
            ics.append(pearson)
        if spearman is not None:
            rank_ics.append(spearman)

        edges = np.quantile(x, np.linspace(0, 1, quantiles + 1))
        buckets: list[float] = []
        for q in range(quantiles):
            lo, hi = edges[q], edges[q + 1]
            member = (x >= lo) & (x <= hi) if q == quantiles - 1 else (x >= lo) & (x < hi)
            buckets.append(float(np.nanmean(y[member])) if member.any() else float("nan"))
        quantile_rows.append(buckets)

    report.dates = len(ics)
    report.mean_symbols = float(np.mean(symbol_counts)) if symbol_counts else 0.0

    if report.dates < MIN_DATES:
        report.failures.append(
            f"only {report.dates} dated observations; {MIN_DATES} needed before an "
            f"average information coefficient is evidence rather than arithmetic"
        )
        return report

    ic_array = np.array(ics, dtype=float)
    report.ic = float(ic_array.mean())
    report.rank_ic = float(np.mean(rank_ics)) if rank_ics else None
    sd = ic_array.std(ddof=1)
    report.ic_ir = float(report.ic / sd) if sd > 0 else None
    report.ic_t_stat = _t_stat(ic_array)

    grid = np.array(quantile_rows, dtype=float)
    # `nanmean` of an all-NaN column warns and returns NaN. A quantile empty on
    # every date is a real outcome — a factor with too few values to fill five
    # buckets — so it is reported as NaN deliberately rather than through a
    # runtime warning nobody reads.
    report.quantile_returns = [_column_mean(grid[:, q]) for q in range(quantiles)]
    diffs = np.diff(report.quantile_returns)
    report.monotonic = bool(np.all(diffs >= 0) or np.all(diffs <= 0))

    spread = grid[:, -1] - grid[:, 0]
    report.spread_return = _column_mean(spread)
    report.spread_t_stat = _t_stat(spread)

    if universe_size:
        report.coverage = float(report.mean_symbols / universe_size)

    report.stability = _stability(scores)
    # Measured, not yet judged. `apply_thresholds` decides between PASS and
    # RESEARCH_ONLY; leaving INSUFFICIENT_DATA here would make every measured
    # factor look unmeasured, because that function declines to judge one.
    report.verdict = Verdict.RESEARCH_ONLY
    return report


def _stability(scores: pl.DataFrame, step: int = 20) -> float | None:
    """Rank correlation of the factor with itself one month later.

    A factor that reshuffles the universe every month is a trading cost
    generator whatever its IC.
    """
    dates = sorted(scores["trading_date"].unique().to_list())
    if len(dates) < step + 1:
        return None
    correlations: list[float] = []
    for i in range(0, len(dates) - step, step):
        left = scores.filter(pl.col("trading_date") == dates[i]).select("symbol", "score")
        right = scores.filter(pl.col("trading_date") == dates[i + step]).select("symbol", "score")
        pair = left.join(right, on="symbol", how="inner", suffix="_later").drop_nulls()
        if pair.height < MIN_SYMBOLS_PER_DATE:
            continue
        value = _spearman(
            pair["score"].to_numpy().astype(float),
            pair["score_later"].to_numpy().astype(float),
        )
        if value is not None:
            correlations.append(value)
    return float(np.mean(correlations)) if correlations else None


def apply_thresholds(report: FactorValidity) -> FactorValidity:
    """Turn measurements into a verdict.

    A factor fails on any single criterion. That is strict on purpose: the cost
    of a weak factor in a composite is not that it adds nothing, it is that it
    adds noise with a plausible story attached.
    """
    if report.verdict in (Verdict.NOT_COMPUTABLE, Verdict.INSUFFICIENT_DATA):
        return report

    failures: list[str] = []
    if report.ic is None or abs(report.ic) < THRESHOLDS["abs_ic"]:
        failures.append(f"|IC| {abs(report.ic or 0):.4f} < {THRESHOLDS['abs_ic']}")
    if report.rank_ic is None or abs(report.rank_ic) < THRESHOLDS["abs_rank_ic"]:
        failures.append(f"|Rank IC| {abs(report.rank_ic or 0):.4f} < {THRESHOLDS['abs_rank_ic']}")
    if report.ic_ir is None or abs(report.ic_ir) < THRESHOLDS["ic_ir"]:
        failures.append(f"|IC IR| {abs(report.ic_ir or 0):.4f} < {THRESHOLDS['ic_ir']}")
    if report.spread_t_stat is None or abs(report.spread_t_stat) < THRESHOLDS["spread_t_stat"]:
        failures.append(
            f"|t(Q5-Q1)| {abs(report.spread_t_stat or 0):.2f} < {THRESHOLDS['spread_t_stat']}"
        )
    if report.coverage is not None and report.coverage < THRESHOLDS["coverage"]:
        failures.append(f"coverage {report.coverage:.2%} < {THRESHOLDS['coverage']:.0%}")
    if report.stability is not None and report.stability < THRESHOLDS["stability"]:
        failures.append(f"stability {report.stability:.2f} < {THRESHOLDS['stability']}")
    if (
        report.max_correlation is not None
        and report.max_correlation > THRESHOLDS["max_correlation"]
    ):
        failures.append(f"correlated {report.max_correlation:.2f} with {report.correlated_with}")

    report.failures = failures
    report.verdict = Verdict.RESEARCH_ONLY if failures else Verdict.PASS
    return report


def correlation_matrix(scores_by_factor: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Pairwise correlation of factor scores across the pooled panel."""
    names = sorted(scores_by_factor)
    if not names:
        return pl.DataFrame()

    merged: pl.DataFrame | None = None
    for name in names:
        part = scores_by_factor[name].select("symbol", "trading_date", pl.col("score").alias(name))
        merged = (
            part
            if merged is None
            else merged.join(part, on=["symbol", "trading_date"], how="full", coalesce=True)
        )
    assert merged is not None

    rows: list[dict[str, Any]] = []
    for a in names:
        row: dict[str, Any] = {"factor": a}
        for b in names:
            if a == b:
                row[b] = 1.0
                continue
            pair = merged.select(a, b).drop_nulls()
            row[b] = (
                _pearson(pair[a].to_numpy().astype(float), pair[b].to_numpy().astype(float))
                if pair.height >= 3
                else None
            )
        rows.append(row)
    return pl.DataFrame(rows)


def attach_correlations(reports: dict[str, FactorValidity], matrix: pl.DataFrame) -> None:
    """Record each factor's closest neighbour, so redundancy is visible."""
    if matrix.is_empty():
        return
    names = [c for c in matrix.columns if c != "factor"]
    for row in matrix.iter_rows(named=True):
        factor = row["factor"]
        report = reports.get(factor)
        if report is None:
            continue
        best_name, best_value = None, 0.0
        for other in names:
            if other == factor or row[other] is None:
                continue
            if abs(row[other]) > abs(best_value):
                best_name, best_value = other, row[other]
        if best_name is not None:
            report.max_correlation = abs(best_value)
            report.correlated_with = best_name


__all__ = [
    "MIN_DATES",
    "MIN_SYMBOLS_PER_DATE",
    "THRESHOLDS",
    "FactorValidity",
    "Verdict",
    "apply_thresholds",
    "attach_correlations",
    "correlation_matrix",
    "evaluate",
    "forward_returns",
]
