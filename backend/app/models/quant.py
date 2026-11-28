"""Derived quant tables: indicators, factor scores, factor validity, versions.

Keyed by `symbol` and `market`, not by a surrogate `stock_id`. `ERD.md` drafted
these against a `stocks` table with an integer key; what was actually built is a
slowly-changing `stock_master` where a symbol has many rows and `id` identifies
a *version* of a company's attributes, not the company. Pointing an indicator at
one of those rows would mean a name change silently reassigns a decade of
history. The symbol is the stable identity here, exactly as it is in
`daily_prices`.

Two columns exist that the ERD did not have, and both answer questions that
would otherwise be unanswerable after the fact:

`price_basis` — whether the numbers were computed from adjusted or raw prices.
Adjustment is currently blocked for historical ranges (no source publishes
decomposable Taiwanese stock dividends before today), so indicators can be
produced on raw prices; a stored indicator that cannot say which basis it used
is one nobody can trust later.

`feature_version` — part of every primary key, so two versions of a factor can
coexist rather than one overwriting the other. Recomputing with a changed
winsorisation threshold produces a new version, and the old scores stay exactly
as any model that consumed them saw them.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, provenance_column


class TechnicalIndicator(Base):
    """One row per symbol per session per feature version.

    Indicators live in JSONB rather than in eighty columns. The list grows every
    time someone asks a new question, and a migration per indicator is not a
    process anybody sustains; the read pattern is "every indicator for this
    symbol on this date", which JSONB serves in one fetch.
    """

    __tablename__ = "technical_indicators"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)

    indicators: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    price_basis: Mapped[str] = mapped_column(String(10), nullable=False, default="ADJUSTED")
    feature_version: Mapped[str] = mapped_column(String(30), nullable=False)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol", "trading_date", "feature_version", name="uq_technical_indicators_key"
        ),
        CheckConstraint("price_basis IN ('ADJUSTED','RAW')", name="price_basis_allowed"),
        CheckConstraint(r"symbol ~ '^[0-9A-Z]{4,10}$'", name="symbol_format"),
        Index("ix_technical_indicators_symbol", "symbol", "trading_date"),
        Index("ix_technical_indicators_date", "trading_date"),
        Index("ix_technical_indicators_gin", "indicators", postgresql_using="gin"),
    )


class FactorScore(Base):
    """A factor's value for one symbol on one date, at every stage.

    Five numbers rather than one. `raw_value` is the quantity as normally
    quoted and is the only one comparable with an outside source; `zscore` is
    what a composite consumes; `percentile` is what a person reads;
    `sector_zscore` answers "good for a bank" rather than "good"; and `score`
    is the one with `direction` applied, so higher is better. Storing only the
    last would make every stored number unverifiable.
    """

    __tablename__ = "factor_scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    factor_name: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)

    raw_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    zscore: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    percentile: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    sector_zscore: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))

    feature_version: Mapped[str] = mapped_column(String(30), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "trading_date",
            "factor_name",
            "feature_version",
            name="uq_factor_scores_key",
        ),
        CheckConstraint(
            "percentile IS NULL OR (percentile >= 0 AND percentile <= 1)",
            name="percentile_range",
        ),
        CheckConstraint(r"symbol ~ '^[0-9A-Z]{4,10}$'", name="symbol_format"),
        Index("ix_factor_scores_symbol", "symbol", "trading_date"),
        Index("ix_factor_scores_ranking", "trading_date", "factor_name", "score"),
    )


class FactorValidityRecord(Base):
    """The report that decides whether a factor may enter a score.

    A row per factor per evaluation, keeping the window it was measured over.
    Overwriting would make it impossible to see that a factor passed in 2019 and
    stopped working in 2024 — which is the single most useful thing this table
    can tell anyone.
    """

    __tablename__ = "factor_validity"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    factor_name: Mapped[str] = mapped_column(String(40), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(30), nullable=False)

    evaluated_from: Mapped[date] = mapped_column(Date, nullable=False)
    evaluated_to: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=20)

    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    metrics: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "factor_name",
            "feature_version",
            "evaluated_from",
            "evaluated_to",
            name="uq_factor_validity_key",
        ),
        CheckConstraint(
            "verdict IN ('PASS','RESEARCH_ONLY','INSUFFICIENT_DATA','NOT_COMPUTABLE')",
            name="verdict_allowed",
        ),
        CheckConstraint("evaluated_to >= evaluated_from", name="validity_window"),
        Index("ix_factor_validity_factor", "factor_name", "evaluated_to"),
    )


class FeatureVersion(Base):
    """The complete, reproducible definition of a feature set.

    `spec` holds every `FactorSpec` as data, so a score can be traced to the
    exact winsorisation, neutralisation and window that produced it. `spec`
    cannot hold the code, which is why `code_version` sits beside it: together
    they say what was intended and what actually ran.
    """

    __tablename__ = "feature_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    version: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    feature_set: Mapped[str] = mapped_column(String(50), nullable=False)

    spec: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    code_version: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    ingestion_id: Mapped[int | None] = provenance_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_feature_versions_set", "feature_set", "created_at"),)


__all__ = [
    "FactorScore",
    "FactorValidityRecord",
    "FeatureVersion",
    "TechnicalIndicator",
]
