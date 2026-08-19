"""What a factor is, declaratively.

A `FactorSpec` is the whole definition: where the raw number comes from, how
much history it needs, how it is cleaned, what it is neutralised against, how it
is standardised, and which direction is good. Nothing about a factor lives in
the job that computes it.

That matters for one specific reason. `feature_versions.spec` stores these
records, so a score computed six months ago can be reproduced exactly — not
"recomputed with today's code and hopefully the same parameters". A factor whose
winsorisation threshold lives in a function body cannot be reproduced from the
database, and the version number recording it would be decoration.

`inputs` names *columns*, not tables, and is checked against the panel before
computing. A factor whose inputs are absent is reported as not computable, with
the missing column named. The alternative — omitting it — makes "this factor
needs financial statements we have not ingested" indistinguishable from "this
factor was quietly dropped".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import polars as pl


class Standardisation(StrEnum):
    ZSCORE = "zscore"
    RANK = "rank"
    SECTOR_ZSCORE = "sector_zscore"


class Neutralisation(StrEnum):
    SECTOR = "sector"
    SIZE = "size"


class FactorCategory(StrEnum):
    VALUE = "value"
    GROWTH = "growth"
    QUALITY = "quality"
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    LIQUIDITY = "liquidity"
    SIZE = "size"
    SENTIMENT = "sentiment"
    NEWS = "news"
    AI_EXPOSURE = "ai_exposure"


@dataclass(frozen=True, slots=True)
class FactorSpec:
    """One factor, completely specified."""

    name: str
    category: FactorCategory
    description: str
    formula: str
    inputs: tuple[str, ...]
    compute: Callable[[pl.DataFrame], pl.Series]
    lookback_days: int
    min_periods: int
    direction: int = 1
    winsorize: tuple[float, float] | None = (0.01, 0.99)
    standardize: Standardisation = Standardisation.ZSCORE
    neutralize: tuple[Neutralisation, ...] = ()

    def __post_init__(self) -> None:
        if self.direction not in (1, -1):
            raise ValueError(f"{self.name}: direction must be +1 or -1")
        if self.min_periods > self.lookback_days:
            raise ValueError(f"{self.name}: min_periods exceeds lookback_days")

    def available(self, columns: Sequence[str]) -> bool:
        return all(column in columns for column in self.inputs)

    def missing(self, columns: Sequence[str]) -> tuple[str, ...]:
        return tuple(column for column in self.inputs if column not in columns)

    def as_dict(self) -> dict[str, Any]:
        """The serialisable record written to `feature_versions.spec`.

        Deliberately excludes `compute`. A callable cannot be stored, and
        pretending otherwise would make the version look reproducible while the
        one part that actually produces the number lives only in the code — which
        is why `code_version` is recorded next to it.
        """
        return {
            "name": self.name,
            "category": str(self.category),
            "description": self.description,
            "formula": self.formula,
            "inputs": list(self.inputs),
            "lookback_days": self.lookback_days,
            "min_periods": self.min_periods,
            "direction": self.direction,
            "winsorize": list(self.winsorize) if self.winsorize else None,
            "standardize": str(self.standardize),
            "neutralize": [str(n) for n in self.neutralize],
        }


@dataclass(frozen=True, slots=True)
class FactorRegistry:
    """Every declared factor, computable or not."""

    specs: dict[str, FactorSpec] = field(default_factory=dict)

    def add(self, spec: FactorSpec) -> None:
        if spec.name in self.specs:
            raise ValueError(f"duplicate factor {spec.name}")
        self.specs[spec.name] = spec

    def __len__(self) -> int:
        return len(self.specs)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.specs.values())

    def __getitem__(self, name: str) -> FactorSpec:
        return self.specs[name]

    def by_category(self, category: FactorCategory) -> list[FactorSpec]:
        return [s for s in self.specs.values() if s.category is category]

    def computable(self, columns: Sequence[str]) -> list[FactorSpec]:
        return [s for s in self.specs.values() if s.available(columns)]

    def not_computable(self, columns: Sequence[str]) -> dict[str, tuple[str, ...]]:
        return {s.name: s.missing(columns) for s in self.specs.values() if not s.available(columns)}

    def max_lookback(self, columns: Sequence[str] | None = None) -> int:
        specs = self.computable(columns) if columns is not None else list(self.specs.values())
        return max((s.lookback_days for s in specs), default=0)


__all__ = [
    "FactorCategory",
    "FactorRegistry",
    "FactorSpec",
    "Neutralisation",
    "Standardisation",
]
