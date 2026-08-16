"""Declarative base and shared column mixins."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming convention so Alembic autogenerate produces stable, reviewable
# constraint names instead of database-assigned ones.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def provenance_column() -> Mapped[int | None]:
    """The link from a stored value back to the bytes that produced it.

    Every canonical row carries one. It is a real foreign key, not a bare
    integer, because "each row can be traced to its source" is the whole point
    of the ingestion design and an unenforced convention drifts.

    `ON DELETE SET NULL`, not `RESTRICT`: raw payloads are the bulkiest thing
    in the database and pruning them is legitimate. Deleting one clears the
    pointer instead of taking the market data with it, leaving `source` and
    `ingested_at` behind — a downgrade from "here are the exact bytes" to
    "here is where it came from and when", rather than a lie.

    Returned from a function rather than declared in a mixin so each model
    gets its own column object; a shared one would be adopted by whichever
    class touched it first.
    """
    return mapped_column(
        BigInteger,
        ForeignKey("raw_ingestions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
