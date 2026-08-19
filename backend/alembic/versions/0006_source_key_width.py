"""006 — room for a corporate action source key that names the endpoint

`corporate_actions.source` and `corporate_action_coverage.source` were 20
characters, which is enough for `TWSE` and not enough for the thing that
actually has to go there.

Coverage is recorded per source and unions across sources, and the three TWSE
corporate action endpoints see different action types: the forecast table
decomposes cash, stock and rights but only ahead of the ex-date, the results
table has history and the exchange's own reference prices but reports a
combined figure it cannot split, and capital reductions are a fourth endpoint
again. If all three wrote `source='TWSE'` they would collide on the natural key
`(symbol, action_type, ex_date, source)` and on
`(symbol, market, source)` in coverage — the forecast's decomposed cash dividend
and the results table's would overwrite each other, and coverage would record
one source's `action_types` over another's. Losing that is not cosmetic: the
union of what each source can see is exactly what `assert_adjustable` reads to
decide whether a symbol may be adjusted at all.

So the source key is the provider code — `TWSE_EXRIGHT_FORECAST` (21),
`TWSE_EXRIGHT_RESULTS` (20), `TWSE_CAPITAL_REDUCTION` (22) — and the columns are
widened to hold it. 30 rather than 40: `data_sources.code` is 40, but nothing
here should grow into an essay, and a bound that is visibly tight is a bound
someone will notice before exceeding.

Six columns, not two. Provenance, quarantine, backfill checkpoints and
ingestion metrics all store the source of a call, and a corporate action run
writes to all of them. Widening only the two corporate action tables produced
exactly the failure this file exists to prevent, one layer over: the actions
were written and the `raw_ingestions` row recording where they came from was
rejected for being 22 characters long.

Widening is not a data migration. Every existing value still fits.

Revision ID: 0006_source_key_width
Revises: 0005_corporate_action_coverage
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_source_key_width"
down_revision: str | None = "0005_corporate_action_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "corporate_actions",
    "corporate_action_coverage",
    "raw_ingestions",
    "data_quarantine",
    "backfill_checkpoints",
    "ingestion_metrics",
)


def upgrade() -> None:
    for table in TABLES:
        op.alter_column(
            table,
            "source",
            existing_type=sa.String(20),
            type_=sa.String(30),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Narrowing truncates rather than fails on PostgreSQL only when the data
    # fits; it does not here, so anything already written under a long source
    # key has to go first. Deleting it is correct rather than destructive: a
    # coverage row is a claim about what was searched, and a claim that can no
    # longer name its source is not one worth keeping.
    for table in TABLES:
        # The table name comes from the tuple above, never from input.
        op.execute(sa.text(f"DELETE FROM {table} WHERE length(source) > 20"))  # noqa: S608
        op.alter_column(
            table,
            "source",
            existing_type=sa.String(30),
            type_=sa.String(20),
            existing_nullable=False,
        )
