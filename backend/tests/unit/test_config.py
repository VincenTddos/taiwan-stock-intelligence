"""Configuration must fail fast on dangerous combinations."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import AppEnv, Settings

# The test session sets APP_ENV, REQUIRE_TIMESCALEDB etc. in the process
# environment (see conftest). These tests are about Settings' own validation
# logic, so they must run against a clean environment.
CONFIG_ENV_VARS = [
    "APP_ENV",
    "DEBUG",
    "ALLOW_MOCK_DATA",
    "JWT_SECRET",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "REQUIRE_TIMESCALEDB",
    "REQUIRE_PGVECTOR",
    "CORS_ORIGINS",
    "ENABLE_LLM",
    "LOG_FORMAT",
    "LOG_LEVEL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _prod(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "APP_ENV": AppEnv.PRODUCTION,
        "JWT_SECRET": "a" * 64,
        "POSTGRES_PASSWORD": "a-real-password",
        "DEBUG": False,
        "ALLOW_MOCK_DATA": False,
        "REQUIRE_TIMESCALEDB": True,
        "REQUIRE_PGVECTOR": True,
        "CORS_ORIGINS": ["https://twquant.example"],
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_local_defaults_are_permissive():
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.APP_ENV is AppEnv.LOCAL
    assert s.ALLOW_MOCK_DATA is True
    assert s.ENABLE_LLM is False, "LLM must be opt-in, never required"


def test_production_rejects_placeholder_jwt_secret():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _prod(JWT_SECRET="dev-secret-not-for-production")


def test_production_rejects_short_jwt_secret():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _prod(JWT_SECRET="short")


def test_production_rejects_debug():
    with pytest.raises(ValidationError, match="DEBUG"):
        _prod(DEBUG=True)


def test_production_rejects_mock_data():
    """The single most important guard in Phase 1: fabricated market data must
    never be servable from a production deployment."""
    with pytest.raises(ValidationError, match="ALLOW_MOCK_DATA"):
        _prod(ALLOW_MOCK_DATA=True)


def test_production_rejects_insecure_cors():
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        _prod(CORS_ORIGINS=["http://localhost:3000"])


def test_production_requires_extensions():
    with pytest.raises(ValidationError, match="REQUIRE_TIMESCALEDB"):
        _prod(REQUIRE_TIMESCALEDB=False)
    with pytest.raises(ValidationError, match="REQUIRE_PGVECTOR"):
        _prod(REQUIRE_PGVECTOR=False)


def test_valid_production_config_constructs():
    s = _prod()
    assert s.is_production is True


def test_database_url_shape():
    s = Settings(
        POSTGRES_USER="u",
        POSTGRES_PASSWORD="p",
        POSTGRES_HOST="h",
        POSTGRES_PORT=5433,
        POSTGRES_DB="d",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert s.database_url == "postgresql+asyncpg://u:p@h:5433/d"
    assert s.database_url_sync == "postgresql://u:p@h:5433/d"


def test_redis_urls_use_distinct_databases():
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    urls = {s.redis_cache_url, s.redis_broker_url, s.redis_result_url}
    assert len(urls) == 3, "cache, broker and result backends must not share a database"
