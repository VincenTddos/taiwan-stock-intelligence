"""Provider registry.

Configuration lives in the `data_sources` table; this module turns a row into a
constructed provider. Nothing else in the codebase instantiates a provider
directly, which is what keeps base URLs and rate limits out of service code.

Selection is explicit rather than magical: `TWQUANT_PROVIDER_MODE` chooses
between live fetching and replaying recorded responses. Defaulting to one or the
other based on whether a network happens to be reachable would make the source
of every number depend on the weather.
"""

from __future__ import annotations

from enum import StrEnum

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppEnv, Settings, get_settings
from app.core.logging import get_logger
from app.models.ops import DataSource, SourceStatus
from app.providers.base import BaseMarketDataProvider, ProviderError, ProviderErrorKind
from app.providers.corporate_actions import CorporateActionProvider
from app.providers.rate_limiter import RateLimitConfig, get_rate_limiter
from app.providers.replay import ReplayProvider
from app.providers.twse import TWSEProvider

log = get_logger(__name__)

# Provider types this codebase can actually construct. A registry row may
# name a source we have not built yet; that must be an error, not a silent
# fallback to something that answers anyway.
IMPLEMENTED_PROVIDER_TYPES = frozenset({"TWSE"})

# Corporate actions are resolved separately from prices. The source that
# publishes a dividend is not necessarily the source that publishes the close,
# and binding them together would make that one decision permanent. Empty until
# Phase 3 lands the first implementation; a lookup against it fails loudly
# rather than falling back to a price provider that cannot answer.
CORPORATE_ACTION_PROVIDER_TYPES: dict[str, type[CorporateActionProvider]] = {}


class ProviderMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


class ProviderRegistry:
    """Builds providers from registry rows."""

    def __init__(
        self,
        settings: Settings | None = None,
        redis: aioredis.Redis | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.limiter = get_rate_limiter(redis)
        self._cache: dict[str, BaseMarketDataProvider] = {}

    # ------------------------------------------------------------------
    @property
    def mode(self) -> ProviderMode:
        return ProviderMode(self.settings.PROVIDER_MODE)

    def _rate_limit_for(self, row: DataSource) -> RateLimitConfig:
        return RateLimitConfig(
            source=row.code,
            requests_per_minute=row.rate_limit_per_minute,
            requests_per_day=row.rate_limit_per_day,
            max_concurrency=row.max_concurrency,
            min_interval_ms=row.min_interval_ms,
        )

    def build(self, row: DataSource) -> BaseMarketDataProvider:
        """Construct the provider described by a registry row."""
        if row.status == SourceStatus.DISABLED:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"source '{row.code}' is disabled in the registry",
                source=row.code,
            )

        # Checked before the mode branch, not inside the live one. Replay used
        # to hand back a ReplayProvider for any code at all, so TPEX, TAIFEX and
        # MOPS — none of which have an implementation or a single recorded
        # response — reported themselves healthy in every replayed run. Replay
        # exists to substitute the transport, not to invent a provider.
        if row.provider_type not in IMPLEMENTED_PROVIDER_TYPES:
            raise ProviderError(
                ProviderErrorKind.NOT_SUPPORTED,
                f"no implementation for provider_type '{row.provider_type}'",
                source=row.code,
            )

        if self.mode is ProviderMode.REPLAY:
            if self.settings.APP_ENV is AppEnv.PRODUCTION:
                raise ProviderError(
                    ProviderErrorKind.CONFIG_ERROR,
                    "PROVIDER_MODE=replay is not permitted in production",
                    source=row.code,
                )
            log.info("provider_replay_mode", source=row.code)
            return ReplayProvider(settings=self.settings)

        if row.provider_type == "TWSE":
            return TWSEProvider(
                limiter=self.limiter,
                rate_limit=self._rate_limit_for(row),
                timeout_seconds=float(row.timeout_seconds),
                max_retries=row.max_retries,
            )

        raise ProviderError(
            ProviderErrorKind.NOT_SUPPORTED,
            f"no implementation for provider_type '{row.provider_type}'",
            source=row.code,
        )

    async def get(self, session: AsyncSession, code: str) -> BaseMarketDataProvider:
        """Fetch the registry row for `code` and build its provider."""
        if code in self._cache:
            return self._cache[code]

        row = (
            await session.execute(select(DataSource).where(DataSource.code == code))
        ).scalar_one_or_none()
        if row is None:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"source '{code}' is not in the registry — add it to data_sources",
                source=code,
            )

        provider = self.build(row)
        self._cache[code] = provider
        return provider

    async def get_corporate_actions(
        self, session: AsyncSession, code: str
    ) -> CorporateActionProvider:
        """Resolve the corporate action provider registered under `code`.

        Deliberately a separate lookup from `get`. Callers ask for the capability
        they need, so no service ends up holding a price provider and hoping it
        also knows about dividends.
        """
        row = (
            await session.execute(select(DataSource).where(DataSource.code == code))
        ).scalar_one_or_none()
        if row is None:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"source '{code}' is not in the registry — add it to data_sources",
                source=code,
            )
        if row.status == SourceStatus.DISABLED:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"source '{code}' is disabled in the registry",
                source=code,
            )

        impl = CORPORATE_ACTION_PROVIDER_TYPES.get(row.provider_type)
        if impl is None:
            raise ProviderError(
                ProviderErrorKind.NOT_SUPPORTED,
                f"no corporate action implementation for provider_type "
                f"'{row.provider_type}' (registered: "
                f"{sorted(CORPORATE_ACTION_PROVIDER_TYPES) or 'none yet'})",
                source=code,
            )
        return impl()

    async def active_sources(self, session: AsyncSession) -> list[DataSource]:
        stmt = (
            select(DataSource)
            .where(DataSource.status.in_([SourceStatus.ACTIVE, SourceStatus.DEGRADED]))
            .order_by(DataSource.code)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def aclose(self) -> None:
        for provider in self._cache.values():
            await provider.aclose()
        self._cache.clear()


__all__ = ["CORPORATE_ACTION_PROVIDER_TYPES", "ProviderMode", "ProviderRegistry"]
