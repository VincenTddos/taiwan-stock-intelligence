"""001 — database extensions

Creates the extensions the platform depends on. TimescaleDB is handled
conditionally: the docker-compose image ships it, but a developer running a
stock apt-installed Postgres does not have it. Rather than making the whole
migration chain unrunnable in that case, we create it when available and record
the outcome. `REQUIRE_TIMESCALEDB` (config) is what enforces its presence in
staging/production, and `/health/database` reports it independently.

Revision ID: 0001_extensions
Revises:
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_extensions"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUIRED = ["vector", "pg_trgm"]
OPTIONAL = ["timescaledb"]


def upgrade() -> None:
    conn = op.get_bind()

    for ext in REQUIRED:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")

    for ext in OPTIONAL:
        # Use a bound parameter via SQLAlchemy text() rather than the raw DBAPI:
        # asyncpg uses $1 placeholders, not %(name)s.
        available = conn.execute(
            sa.text("SELECT 1 FROM pg_available_extensions WHERE name = :n"), {"n": ext}
        ).scalar()
        if available:
            op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        else:
            print(
                f"[alembic] extension '{ext}' is not available on this server; skipping. "
                "This is expected on a plain Postgres install and is reported as "
                "DEGRADED by /health/database. Staging/production must use the "
                "timescaledb image."
            )


def downgrade() -> None:
    # Extensions are deliberately not dropped: other databases in the cluster or
    # objects created outside this migration chain may depend on them, and
    # dropping an extension cascades. Removal is an explicit operator action.
    pass
