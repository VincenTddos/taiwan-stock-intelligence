from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_can_connect(session):
    assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_pgvector_extension_installed(session):
    version = (
        await session.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
    ).scalar_one_or_none()
    assert version is not None, (
        "pgvector is required. Run migration 0001_extensions, or use the "
        "timescale/timescaledb-ha image which bundles it."
    )


async def test_pgvector_is_usable(session):
    """Installed is not the same as working — exercise the type and operator."""
    await session.execute(text("CREATE TEMP TABLE _v (id int, e vector(3))"))
    await session.execute(text("INSERT INTO _v VALUES (1,'[1,0,0]'), (2,'[0,1,0]')"))
    nearest = (
        await session.execute(text("SELECT id FROM _v ORDER BY e <=> '[1,0,0]' LIMIT 1"))
    ).scalar_one()
    assert nearest == 1


async def test_timescaledb_reported_honestly(session, settings):
    """TimescaleDB is required in staging/production. Locally it may be absent;
    the point of this test is that we never *silently* assume it is there."""
    version = (
        await session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")
        )
    ).scalar_one_or_none()

    if settings.REQUIRE_TIMESCALEDB:
        assert version is not None, "REQUIRE_TIMESCALEDB=true but the extension is missing"
    elif version is None:
        pytest.skip("timescaledb not installed and not required in this environment")


async def test_timescale_hypertable_roundtrip_when_available(session):
    version = (
        await session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")
        )
    ).scalar_one_or_none()
    if version is None:
        pytest.skip("timescaledb not installed")

    await session.execute(
        text("CREATE TABLE IF NOT EXISTS _ts_probe (ts timestamptz NOT NULL, v double precision)")
    )
    await session.execute(
        text("SELECT create_hypertable('_ts_probe','ts',if_not_exists=>TRUE,migrate_data=>TRUE)")
    )
    count = (
        await session.execute(
            text(
                "SELECT count(*) FROM timescaledb_information.hypertables "
                "WHERE hypertable_name='_ts_probe'"
            )
        )
    ).scalar_one()
    await session.execute(text("DROP TABLE _ts_probe"))
    assert count == 1


async def test_timestamps_are_timezone_aware(session):
    """Naive timestamps are how timezone bugs enter a trading system."""
    from app.core.security import hash_password
    from app.models.user import User

    user = User(email="tz@test.dev", password_hash=hash_password("password-123"), role="viewer")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    assert user.created_at.tzinfo is not None


async def test_users_role_check_constraint(session):
    from sqlalchemy.exc import DBAPIError

    from app.core.security import hash_password

    await session.execute(
        text(
            "ALTER TABLE users ADD CONSTRAINT ck_users_role_allowed "
            "CHECK (role IN ('admin','analyst','viewer'))"
        )
    )
    with pytest.raises(DBAPIError):
        await session.execute(
            text("INSERT INTO users (email,password_hash,role) VALUES (:e,:p,'superuser')"),
            {"e": "bad@test.dev", "p": hash_password("x")},
        )
    await session.rollback()
