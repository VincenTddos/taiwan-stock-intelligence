"""005 — corporate action fields for the classes Phase 3 must adjust for

`corporate_actions` already carried cash dividends, stock dividends, split
ratios and rights subscription terms. Three columns were missing before it could
represent every class the adjustment pipeline has to handle.

`cash_returned_per_share` — a Taiwanese cash capital reduction (現金減資) both
returns money to holders and cancels shares, so it needs a cash amount alongside
`split_ratio`. Reusing `cash_dividend` for it would have been convenient and
wrong: a dividend distributes earnings, a reduction returns capital, they are
taxed differently, and once merged there is no way to tell afterwards which
event actually occurred. A loss-offsetting reduction (彌補虧損減資) returns no
cash and leaves this null, which is why it is nullable rather than defaulted.

`reference_price_before` / `reference_price_after` — the exchange publishes its
own pre- and post-event reference price (除權息前收盤價 / 除權息參考價). Storing
them does not change how the adjustment factor is computed; it makes the
computed factor checkable against the authority that set it. Without that, an
adjustment is asserted rather than demonstrated, and a wrong one is invisible
until it has already contaminated a return series.

No data migration: every existing row keeps its meaning, and the new columns are
null for actions ingested before a source that supplies them existed.

Revision ID: 0005_corporate_action_coverage
Revises: 0004_enforce_provenance_fk
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_corporate_action_coverage"
down_revision: str | None = "0004_enforce_provenance_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "corporate_actions"

NEW_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("cash_returned_per_share", sa.Numeric(18, 6)),
    ("reference_price_before", sa.Numeric(18, 4)),
    ("reference_price_after", sa.Numeric(18, 4)),
)


def upgrade() -> None:
    for name, type_ in NEW_COLUMNS:
        op.add_column(TABLE, sa.Column(name, type_, nullable=True))

    # Coverage is recorded as a positive fact so that an empty result stops
    # being ambiguous. "No dividend was paid" and "nobody ever fetched
    # dividends for this symbol" are otherwise the same empty set, and
    # adjusting on the first reading when the truth is the second deletes a
    # real adjustment while leaving a return series that looks fine.
    op.create_table(
        "corporate_action_coverage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("covered_from", sa.Date(), nullable=False),
        sa.Column("covered_to", sa.Date(), nullable=False),
        sa.Column("action_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actions_found", sa.Integer(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("covered_to >= covered_from", name="coverage_window"),
        sa.CheckConstraint("actions_found >= 0", name="actions_found_non_negative"),
        sa.ForeignKeyConstraint(
            ["ingestion_id"],
            ["raw_ingestions.id"],
            name="fk_corporate_action_coverage_ingestion_id_raw_ingestions",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_corporate_action_coverage"),
        sa.UniqueConstraint("symbol", "market", "source", name="uq_corporate_action_coverage_key"),
    )
    op.create_index(
        "ix_corporate_action_coverage_symbol",
        "corporate_action_coverage",
        ["symbol", "market"],
    )
    op.create_index(
        "ix_corporate_action_coverage_ingestion_id",
        "corporate_action_coverage",
        ["ingestion_id"],
    )

    # A reference price of zero or below is not a price. The constraint is on
    # the stored value rather than left to the parser, because these columns
    # exist specifically to validate the adjustment — a validator that can hold
    # nonsense is not one worth checking against.
    op.create_check_constraint(
        "reference_prices_positive",
        TABLE,
        "(reference_price_before IS NULL OR reference_price_before > 0) AND "
        "(reference_price_after IS NULL OR reference_price_after > 0)",
    )
    op.create_check_constraint(
        "cash_returned_non_negative",
        TABLE,
        "cash_returned_per_share IS NULL OR cash_returned_per_share >= 0",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_corporate_action_coverage_ingestion_id", table_name="corporate_action_coverage"
    )
    op.drop_index("ix_corporate_action_coverage_symbol", table_name="corporate_action_coverage")
    op.drop_table("corporate_action_coverage")

    # Bare names, not `ck_corporate_actions_...`. The metadata naming convention
    # is active here, so alembic expands whatever is passed — handing it an
    # already-expanded name produces `ck_corporate_actions_ck_corporate_...`,
    # truncated to fit, matching nothing.
    op.drop_constraint("cash_returned_non_negative", TABLE, type_="check")
    op.drop_constraint("reference_prices_positive", TABLE, type_="check")
    for name, _ in reversed(NEW_COLUMNS):
        op.drop_column(TABLE, name)
