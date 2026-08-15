from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest

# Test settings must be in place before app.core.config is first imported.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("POSTGRES_DB", os.getenv("POSTGRES_TEST_DB", "twquant_test"))
os.environ.setdefault("REQUIRE_TIMESCALEDB", "false")
os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("JWT_SECRET", "test-secret-key-that-is-long-enough-for-tests-0123456789")

import httpx
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import close_redis, get_redis
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import dispose_engine, get_engine, get_sessionmaker
from app.main import create_app
from app.models.user import Role, User


def _service_available(host: str, port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(autouse=True)
async def _reset_redis_client():
    """The app keeps one module-level Redis client, which is correct in
    production but binds it to whichever event loop created it. pytest-asyncio
    gives each test a fresh loop, so the client must be dropped between tests
    or the second test sees 'Event loop is closed'."""
    yield
    await close_redis()


@pytest.fixture(scope="session")
def postgres_available(settings) -> bool:
    return _service_available(settings.POSTGRES_HOST, settings.POSTGRES_PORT)


@pytest.fixture(scope="session")
def redis_available(settings) -> bool:
    return _service_available(settings.REDIS_HOST, settings.REDIS_PORT)


@pytest.fixture(scope="session", autouse=True)
def _require_services(postgres_available, redis_available):
    """Integration tests need real services; skip loudly rather than pass silently."""
    return {"postgres": postgres_available, "redis": redis_available}


@pytest.fixture
async def db_schema(postgres_available):
    if not postgres_available:
        pytest.skip("PostgreSQL not reachable")
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await dispose_engine()


@pytest.fixture
async def session(db_schema) -> AsyncGenerator[AsyncSession, None]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
async def admin_user(session) -> User:
    user = User(
        email="admin@test.dev",
        password_hash=hash_password("admin-password-123"),
        display_name="Test Admin",
        role=Role.ADMIN,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture
async def viewer_user(session) -> User:
    user = User(
        email="viewer@test.dev",
        password_hash=hash_password("viewer-password-123"),
        display_name="Test Viewer",
        role=Role.VIEWER,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture
async def client(db_schema, redis_available) -> AsyncGenerator[httpx.AsyncClient, None]:
    if not redis_available:
        pytest.skip("Redis not reachable")
    app = create_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    await close_redis()


@pytest.fixture
async def redis_client(redis_available, settings):
    if not redis_available:
        pytest.skip("Redis not reachable")
    client = get_redis(settings)
    await client.flushdb()
    yield client


async def login(client: httpx.AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    resp.raise_for_status()
    return str(resp.json()["data"]["access_token"])
