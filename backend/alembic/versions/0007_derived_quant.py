"""007 — derived quant: indicators, factor scores, factor validity, feature versions

Four tables, and three decisions inside them worth stating before someone reads
the columns and wonders.

**Keyed by symbol, not by a surrogate id.** `ERD.md` drafted these against a
`stocks` table with an integer key. What was built is a slowly-changing
`stock_master` where `id` identifies a *version* of a company"s attributes — so
pointing a decade of indicators at one of those rows would mean a name change
silently reassigns history. `daily_prices` already keys on symbol; these follow.

**`feature_version` is part of every natural key.** Recomputing with a changed
window or a changed winsorisation threshold produces a new version alongside the
old one rather than overwriting it. A model trained last quarter can still be
shown the exact numbers it was trained on, which is the only way its metrics
remain checkable.

**`price_basis` is stored per row.** Indicators are meant to run on prices
adjusted for corporate actions. Adjustment is currently blocked over historical
ranges — no source publishes decomposable Taiwanese stock dividends before today
— so indicators can legitimately be computed on raw prices, and a row that
cannot say which basis it used is one nobody can trust later. It is a column
rather than a note in a report because reports are not joined to.

No TimescaleDB hypertables here yet. These tables are empty, the extension is
optional in local development (ADR-015), and converting them later is a
migration that takes a lock on empty tables — doing it now would make the
extension a hard requirement for a table nobody is querying.

Revision ID: 0007_derived_quant
Revises: 0006_source_key_width
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_derived_quant"
down_revision: str | None = "0006_source_key_width"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "factor_scores",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("factor_name", sa.String(length=40), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("raw_value", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("zscore", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("percentile", sa.Numeric(precision=8, scale=6), nullable=True),
        sa.Column("sector_zscore", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("feature_version", sa.String(length=30), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            r"symbol ~ '^[0-9A-Z]{4,10}$'", name=op.f("ck_factor_scores_symbol_format")
        ),
        sa.CheckConstraint(
            "percentile IS NULL OR (percentile >= 0 AND percentile <= 1)",
            name=op.f("ck_factor_scores_percentile_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_factor_scores")),
        sa.UniqueConstraint(
            "symbol", "trading_date", "factor_name", "feature_version", name="uq_factor_scores_key"
        ),
    )
    op.create_index(
        "ix_factor_scores_ranking",
        "factor_scores",
        ["trading_date", "factor_name", "score"],
        unique=False,
    )
    op.create_index(
        "ix_factor_scores_symbol", "factor_scores", ["symbol", "trading_date"], unique=False
    )
    op.create_table(
        "factor_validity",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("factor_name", sa.String(length=40), nullable=False),
        sa.Column("feature_version", sa.String(length=30), nullable=False),
        sa.Column("evaluated_from", sa.Date(), nullable=False),
        sa.Column("evaluated_to", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "verdict IN ('PASS','RESEARCH_ONLY','INSUFFICIENT_DATA','NOT_COMPUTABLE')",
            name=op.f("ck_factor_validity_verdict_allowed"),
        ),
        sa.CheckConstraint(
            "evaluated_to >= evaluated_from", name=op.f("ck_factor_validity_validity_window")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_factor_validity")),
        sa.UniqueConstraint(
            "factor_name",
            "feature_version",
            "evaluated_from",
            "evaluated_to",
            name="uq_factor_validity_key",
        ),
    )
    op.create_index(
        "ix_factor_validity_factor",
        "factor_validity",
        ["factor_name", "evaluated_to"],
        unique=False,
    )
    op.create_table(
        "technical_indicators",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("indicators", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("price_basis", sa.String(length=10), nullable=False),
        sa.Column("feature_version", sa.String(length=30), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "price_basis IN ('ADJUSTED','RAW')",
            name=op.f("ck_technical_indicators_price_basis_allowed"),
        ),
        sa.CheckConstraint(
            r"symbol ~ '^[0-9A-Z]{4,10}$'", name=op.f("ck_technical_indicators_symbol_format")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_technical_indicators")),
        sa.UniqueConstraint(
            "symbol", "trading_date", "feature_version", name="uq_technical_indicators_key"
        ),
    )
    op.create_index(
        "ix_technical_indicators_date", "technical_indicators", ["trading_date"], unique=False
    )
    op.create_index(
        "ix_technical_indicators_gin",
        "technical_indicators",
        ["indicators"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_technical_indicators_symbol",
        "technical_indicators",
        ["symbol", "trading_date"],
        unique=False,
    )
    op.create_table(
        "feature_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.String(length=30), nullable=False),
        sa.Column("feature_set", sa.String(length=50), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("code_version", sa.String(length=40), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("ingestion_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_id"],
            ["raw_ingestions.id"],
            name=op.f("fk_feature_versions_ingestion_id_raw_ingestions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_versions")),
        sa.UniqueConstraint("version", name=op.f("uq_feature_versions_version")),
    )
    op.create_index(
        op.f("ix_feature_versions_ingestion_id"), "feature_versions", ["ingestion_id"], unique=False
    )
    op.create_index(
        "ix_feature_versions_set", "feature_versions", ["feature_set", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_feature_versions_set", table_name="feature_versions")
    op.drop_index(op.f("ix_feature_versions_ingestion_id"), table_name="feature_versions")
    op.drop_table("feature_versions")
    op.drop_index("ix_technical_indicators_symbol", table_name="technical_indicators")
    op.drop_index(
        "ix_technical_indicators_gin", table_name="technical_indicators", postgresql_using="gin"
    )
    op.drop_index("ix_technical_indicators_date", table_name="technical_indicators")
    op.drop_table("technical_indicators")
    op.drop_index("ix_factor_validity_factor", table_name="factor_validity")
    op.drop_table("factor_validity")
    op.drop_index("ix_factor_scores_symbol", table_name="factor_scores")
    op.drop_index("ix_factor_scores_ranking", table_name="factor_scores")
    op.drop_table("factor_scores")
