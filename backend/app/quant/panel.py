"""The price panel: the one shape everything in `app.quant` consumes.

A panel is a polars frame of daily bars for one or many symbols, sorted by
symbol and date, carrying the benchmark alongside each row so that market-
relative indicators need no second frame and no join at compute time.

Three properties are enforced rather than assumed, because each one has a
failure mode that produces plausible numbers rather than an error:

**Sorted, ascending, no duplicates.** Every rolling window in this package
assumes chronological order. Given rows out of order, `rolling_mean` still
returns numbers — just the wrong ones — and nothing downstream can tell.

**A stated price basis.** Indicators are supposed to run on prices adjusted for
corporate actions; on an ex-dividend date an unadjusted series shows a gap that
is not a return. Right now the adjustment is blocked for historical ranges (no
source publishes decomposable Taiwanese stock dividends before today), so the
panel carries `price_basis` and every row computed from it records which basis
was used. A number that cannot say whether it was adjusted is a number nobody
can check later.

**Suspended sessions excluded.** A halted stock's zero-volume bar is not a
missing observation and not a real one; leaving it in makes volatility collapse
and volume ratios explode on resumption.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import polars as pl

REQUIRED_COLUMNS = (
    "symbol",
    "trading_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

OPTIONAL_COLUMNS = (
    "turnover",
    "benchmark_close",
    "shares_outstanding",
    "sector",
    "is_suspended",
)


class PriceBasis(StrEnum):
    """Which price series the indicators were computed from."""

    #: Adjusted for corporate actions — the only basis a return series may use.
    ADJUSTED = "ADJUSTED"
    #: As published. Legitimate for intraday-shape indicators, and a hazard for
    #: anything spanning an ex-date. Recorded so it can never be mistaken.
    RAW = "RAW"


class PanelError(ValueError):
    """The panel does not satisfy something the maths depends on."""


@dataclass(frozen=True, slots=True)
class Panel:
    """A validated price panel and the basis its prices are on."""

    frame: pl.DataFrame
    price_basis: PriceBasis
    benchmark: str | None = None

    @property
    def symbols(self) -> list[str]:
        return sorted(self.frame["symbol"].unique().to_list())

    def for_symbol(self, symbol: str) -> pl.DataFrame:
        return self.frame.filter(pl.col("symbol") == symbol)

    def truncated(self, as_of: date) -> Panel:
        """The panel as it stood at `as_of`.

        Central to the look-ahead tests: the poisoned frame and the clean one
        must be identical after truncation, so every indicator computed from a
        truncated panel is provably blind to what came later.
        """
        return Panel(
            frame=self.frame.filter(pl.col("trading_date") <= as_of),
            price_basis=self.price_basis,
            benchmark=self.benchmark,
        )


def validate(frame: pl.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise PanelError(f"panel is missing required columns: {missing}")

    if frame.is_empty():
        return

    ordered = frame.select("symbol", "trading_date").sort(["symbol", "trading_date"])
    if not ordered.equals(frame.select("symbol", "trading_date")):
        raise PanelError("panel must be sorted by (symbol, trading_date) ascending")

    duplicates = frame.select("symbol", "trading_date").is_duplicated().sum()
    if duplicates:
        raise PanelError(f"panel has {duplicates} duplicate (symbol, trading_date) rows")


def build(
    frame: pl.DataFrame,
    *,
    price_basis: PriceBasis,
    benchmark: str | None = None,
    drop_suspended: bool = True,
) -> Panel:
    """Validate and wrap a frame.

    Suspended sessions are dropped rather than zero-filled. A halt is an absence
    of trading, not a day of no movement, and treating it as the latter puts a
    run of zero returns into every volatility estimate that spans it.
    """
    if "is_suspended" in frame.columns and drop_suspended:
        frame = frame.filter(~pl.col("is_suspended").fill_null(False))
    frame = frame.sort(["symbol", "trading_date"])
    validate(frame)
    return Panel(frame=frame, price_basis=price_basis, benchmark=benchmark)


__all__ = [
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "Panel",
    "PanelError",
    "PriceBasis",
    "build",
    "validate",
]
