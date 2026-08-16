"""004 — enforce the provenance chain with foreign keys

Phase 2's central claim is that no stored number exists without recorded
bytes behind it: every canonical row carries `ingestion_id`, and the
`raw_ingestions` row it points at holds the endpoint, the request and
response timestamps, the byte count and the checksum.

Until this migration that chain was a convention, not a constraint. The
columns were plain BigIntegers with no referential integrity, so an
`ingestion_id` could point at a row that never existed or had been removed,
and the database would happily agree. A promise the schema does not enforce
is a promise that decays.

`ON DELETE SET NULL` rather than `RESTRICT` on purpose. Raw payloads are the
bulkiest thing in the database and pruning old ones is a legitimate
operation; `RESTRICT` would make retention policy impossible without
deleting market data along with it. `SET NULL` keeps the canonical row,
drops the dangling pointer, and leaves `source` and `ingested_at` in place —
so a pruned row degrades from "here are the exact bytes" to "here is where
it came from and when", which is honest, rather than pointing at nothing.

The columns stay nullable for the same reason.

Revision ID: 0004_enforce_provenance_fk
Revises: 0003_market_data_infrastructure
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_enforce_provenance_fk"
down_revision: str | None = "0003_market_data_infrastructure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table that records where its data came from.
PROVENANCE_TABLES: tuple[str, ...] = (
    "stock_master",
    "daily_prices",
    "index_quotes",
    "institutional_flow",
    "corporate_actions",
    "data_quarantine",
    "ingestion_metrics",
)


# Names follow NAMING_CONVENTION in app/db/base.py, so what autogenerate would
# have produced and what this migration creates are the same string. They must
# match exactly or `alembic check` reports permanent drift.
def _fk(table: str) -> str:
    return f"fk_{table}_ingestion_id_raw_ingestions"


def _ix(table: str) -> str:
    return f"ix_{table}_ingestion_id"


def _clear_dangling(table: str) -> None:
    """Null out pointers that lead nowhere, so the constraint can be created.

    On a database built by 0003 this matches nothing. On one that has been
    running it surfaces the damage as NULLs rather than aborting the migration
    — which is the same choice the quality engine makes everywhere else: mark
    it, don't drop the row, don't pretend.

    Built from SQLAlchemy constructs rather than an f-string because a table
    name cannot be a bound parameter, and interpolating identifiers by hand is
    the habit worth not having even when the values come from a constant.
    """
    tbl = sa.table(table, sa.column("ingestion_id"))
    raw = sa.table("raw_ingestions", sa.column("id"))
    op.execute(
        tbl.update()
        .where(tbl.c.ingestion_id.is_not(None))
        .where(tbl.c.ingestion_id.not_in(sa.select(raw.c.id)))
        .values(ingestion_id=None)
    )


def upgrade() -> None:
    for table in PROVENANCE_TABLES:
        _clear_dangling(table)
        op.create_foreign_key(
            _fk(table),
            table,
            "raw_ingestions",
            ["ingestion_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(_ix(table), table, ["ingestion_id"])


def downgrade() -> None:
    for table in PROVENANCE_TABLES:
        op.drop_index(_ix(table), table_name=table)
        op.drop_constraint(_fk(table), table, type_="foreignkey")
