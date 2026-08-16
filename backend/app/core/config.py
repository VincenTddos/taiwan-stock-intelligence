"""Application configuration.

Every setting comes from the environment (12-factor). Nothing is hard-coded,
and `Settings` refuses to construct itself if the environment is internally
inconsistent — a bad config should fail at boot, not at 03:00 during an
ingestion job.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


# Placeholder secrets shipped in .env.example. Refusing to boot production with
# any of these is cheaper than discovering it after an incident.
INSECURE_SECRETS: frozenset[str] = frozenset(
    {
        "change-me",
        "changeme",
        "secret",
        "dev-secret-not-for-production",
        "",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ---------------------------------------------------------------- app
    APP_NAME: str = "twquant"
    APP_ENV: AppEnv = AppEnv.LOCAL
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    TIMEZONE: str = "Asia/Taipei"

    # ---------------------------------------------------------------- db
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "twquant"
    POSTGRES_PASSWORD: str = "twquant_dev"
    POSTGRES_DB: str = "twquant"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # Extensions we expect the cluster to provide. TimescaleDB is not available
    # in every local dev setup (e.g. a plain apt-installed Postgres), so it is
    # required by default but can be relaxed for local work. It is *always*
    # required in staging/production — enforced in `_check_consistency`.
    REQUIRE_TIMESCALEDB: bool = True
    REQUIRE_PGVECTOR: bool = True

    # ---------------------------------------------------------------- redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str | None = None
    REDIS_CACHE_DB: int = 0
    REDIS_BROKER_DB: int = 1
    REDIS_RESULT_DB: int = 2

    # ---------------------------------------------------------------- auth
    JWT_SECRET: str = "dev-secret-not-for-production"
    JWT_ALGORITHM: Literal["HS256", "HS512"] = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 7

    # ---------------------------------------------------------------- cors
    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # ---------------------------------------------------------------- data
    # Phase 1 has no market data at all. This flag exists so that the guarantee
    # "MockProvider must never run in production" is enforceable from day one.
    ALLOW_MOCK_DATA: bool = True

    # ------------------------------------------------------------ providers
    # How market data is obtained. `live` fetches from the exchange; `replay`
    # serves recorded genuine responses through the same parsers (see
    # providers/replay.py). Chosen explicitly rather than inferred from network
    # reachability — the origin of a number must not depend on the weather.
    PROVIDER_MODE: Literal["live", "replay"] = "live"
    PROVIDER_USER_AGENT_CONTACT: str = ""

    # ---------------------------------------------------------------- llm
    # The whole point of ADR-011: the platform must work with the LLM switched
    # off. Nothing in core (market data, quant, db, api, backtest) may depend
    # on this being true.
    ENABLE_LLM: bool = False
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # ---------------------------------------------------------------- obs
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"

    # ---------------------------------------------------------------- health
    HEALTH_TIMEOUT_SECONDS: float = 5.0
    WORKER_HEARTBEAT_TTL_SECONDS: int = 120

    # ------------------------------------------------------------ computed
    @property
    def database_url(self) -> str:
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                host=self.POSTGRES_HOST,
                port=self.POSTGRES_PORT,
                path=self.POSTGRES_DB,
            )
        )

    @property
    def database_url_sync(self) -> str:
        """psycopg-free sync URL, used by Alembic's offline mode and tooling."""
        return str(
            PostgresDsn.build(
                scheme="postgresql",
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                host=self.POSTGRES_HOST,
                port=self.POSTGRES_PORT,
                path=self.POSTGRES_DB,
            )
        )

    def _redis_url(self, db: int) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{db}"

    @property
    def redis_cache_url(self) -> str:
        return self._redis_url(self.REDIS_CACHE_DB)

    @property
    def redis_broker_url(self) -> str:
        return self._redis_url(self.REDIS_BROKER_DB)

    @property
    def redis_result_url(self) -> str:
        return self._redis_url(self.REDIS_RESULT_DB)

    @property
    def is_production(self) -> bool:
        return self.APP_ENV is AppEnv.PRODUCTION

    # ------------------------------------------------------- consistency
    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        """Fail fast on configurations that are dangerous rather than merely wrong."""
        if self.APP_ENV in (AppEnv.PRODUCTION, AppEnv.STAGING):
            if self.JWT_SECRET in INSECURE_SECRETS or len(self.JWT_SECRET) < 32:
                raise ValueError(
                    f"JWT_SECRET is a placeholder or too short for APP_ENV={self.APP_ENV}. "
                    "Generate one with: python -c 'import secrets;print(secrets.token_urlsafe(64))'"
                )
            if self.POSTGRES_PASSWORD in INSECURE_SECRETS:
                raise ValueError(f"POSTGRES_PASSWORD is a placeholder for APP_ENV={self.APP_ENV}.")
            if not self.REQUIRE_TIMESCALEDB:
                raise ValueError("REQUIRE_TIMESCALEDB cannot be disabled outside local/test.")
            if not self.REQUIRE_PGVECTOR:
                raise ValueError("REQUIRE_PGVECTOR cannot be disabled outside local/test.")

        if self.is_production:
            if self.DEBUG:
                raise ValueError("DEBUG must be false in production.")
            if self.ALLOW_MOCK_DATA:
                raise ValueError(
                    "ALLOW_MOCK_DATA must be false in production. "
                    "Mock/demo data must never be served as real market data."
                )
            if any(o.startswith("http://") for o in self.CORS_ORIGINS):
                raise ValueError("CORS_ORIGINS must use https in production.")
            if self.PROVIDER_MODE != "live":
                raise ValueError(
                    "PROVIDER_MODE must be 'live' in production. Recorded responses are "
                    "genuine but historical; serving them would misrepresent freshness."
                )

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
