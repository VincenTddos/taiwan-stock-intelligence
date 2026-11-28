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

from dataclasses import replace
from enum import StrEnum

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppEnv, Settings, get_settings
from app.core.logging import get_logger
from app.models.news import NewsSource
from app.models.ops import DataSource, SourceStatus
from app.providers.base import BaseMarketDataProvider, ProviderError, ProviderErrorKind
from app.providers.corporate_actions import CorporateActionProvider
from app.providers.http import HttpFetcher
from app.providers.news import NewsProvider
from app.providers.rate_limiter import RateLimitConfig, get_rate_limiter
from app.providers.replay import (
    ReplayProvider,
    replay_corporate_action_provider,
    replay_news_provider,
)
from app.providers.twse import TWSEProvider
from app.providers.twse_corporate_actions import (
    TWSEForecastProvider,
    TWSEReductionProvider,
    TWSEResultsProvider,
)
from app.providers.twse_news import TWSEMaterialInformationProvider, TWSENewsProvider

log = get_logger(__name__)

# Provider types this codebase can actually construct. A registry row may
# name a source we have not built yet; that must be an error, not a silent
# fallback to something that answers anyway.
IMPLEMENTED_PROVIDER_TYPES = frozenset({"TWSE"})

# Corporate actions are resolved separately from prices. The source that
# publishes a dividend is not necessarily the source that publishes the close,
# and binding them together would make that one decision permanent.
#
# Three TWSE entries, not one, because no single endpoint can drive an
# adjustment: the forecast table decomposes an event but only ahead of time, the
# results table has history and the exchange's own reference prices but reports
# a combined figure it cannot split, and capital reductions are elsewhere again.
# Each declares `supported_actions` as narrowly as its data justifies, and
# coverage unions across them.
CORPORATE_ACTION_PROVIDER_TYPES: dict[str, type[CorporateActionProvider]] = {
    "TWSE_EXRIGHT_FORECAST": TWSEForecastProvider,
    "TWSE_EXRIGHT_RESULTS": TWSEResultsProvider,
    "TWSE_CAPITAL_REDUCTION": TWSEReductionProvider,
}


# News sources are resolved from `news_sources`, a different table from
# `data_sources`, because what has to be recorded about them is different: a
# price endpoint needs a rate limit, a news source needs a reason it is
# permissible to read at all.
NEWS_PROVIDER_TYPES: dict[str, type[NewsProvider]] = {
    "TWSE_MATERIAL": TWSEMaterialInformationProvider,
    "TWSE_NEWS": TWSENewsProvider,
}


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

        # Checked here and not only in `build`: this lookup used to return a
        # live provider whatever the mode said, so a replayed run reached the
        # exchange for corporate actions while reporting `transport=REPLAY`
        # everywhere else. Mode is a property of the application, not of one
        # dataset.
        if self.mode is ProviderMode.REPLAY:
            if self.settings.APP_ENV is AppEnv.PRODUCTION:
                raise ProviderError(
                    ProviderErrorKind.CONFIG_ERROR,
                    "PROVIDER_MODE=replay is not permitted in production",
                    source=code,
                )
            log.info("provider_replay_mode", source=code, dataset="corporate_actions")
            return replay_corporate_action_provider(
                row.provider_type, CORPORATE_ACTION_PROVIDER_TYPES, settings=self.settings
            )

        # The transport is built here, from the row. Constructing `impl()` bare
        # let each provider fall back to its own hard-coded timeout and retry
        # count, so the registry's whole purpose — that a timeout is a row
        # update rather than a deploy — did not apply to this dataset. It
        # mattered the first time it was run live: the capital reduction
        # endpoint needs far longer than the 20 s default.
        return impl(
            fetcher=HttpFetcher(
                source=row.code,
                base_url=row.base_url,
                rate_limit=self._corporate_action_rate_limit(row),
                limiter=self.limiter,
                timeout_seconds=float(row.timeout_seconds),
                max_retries=row.max_retries,
            )
        )

    def _corporate_action_rate_limit(self, row: DataSource) -> RateLimitConfig:
        """One budget per exchange, not one per endpoint.

        Corporate action backfill runs alongside daily ingestion, and a budget
        per endpoint would triple the rate TWSE sees from us at exactly the
        moment we are busiest. The limits themselves still come from the row.
        """
        return replace(self._rate_limit_for(row), source=row.market or row.code)

    async def get_news(self, session: AsyncSession, code: str) -> NewsProvider:
        """Resolve a news provider, refusing one nobody established we may read.

        Two gates rather than one. The registry row must be active — which the
        database already refuses unless a robots policy, a licence note and a
        verification date are all recorded — and the provider itself must pass
        `assert_permitted`. The duplication is deliberate: the row says what was
        decided, the class says what the code will actually do, and a provider
        pointed at a different path than the one that was checked should fail on
        its own account rather than inherit someone else's approval.
        """
        row = (
            await session.execute(select(NewsSource).where(NewsSource.code == code))
        ).scalar_one_or_none()
        if row is None:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"news source '{code}' is not in the registry — add it to news_sources",
                source=code,
            )
        if not row.is_active:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"news source '{code}' is not active. A source is switched on only "
                f"after its robots policy, licence and verification date are recorded.",
                source=code,
            )

        impl = NEWS_PROVIDER_TYPES.get(row.provider_type)
        if impl is None:
            raise ProviderError(
                ProviderErrorKind.NOT_SUPPORTED,
                f"no news implementation for provider_type '{row.provider_type}' "
                f"(registered: {sorted(NEWS_PROVIDER_TYPES) or 'none yet'})",
                source=code,
            )

        if self.mode is ProviderMode.REPLAY:
            if self.settings.APP_ENV is AppEnv.PRODUCTION:
                raise ProviderError(
                    ProviderErrorKind.CONFIG_ERROR,
                    "PROVIDER_MODE=replay is not permitted in production",
                    source=code,
                )
            log.info("provider_replay_mode", source=code, dataset="news")
            provider = replay_news_provider(
                row.provider_type, NEWS_PROVIDER_TYPES, settings=self.settings
            )
            provider.assert_permitted()
            return provider

        provider = impl(
            fetcher=HttpFetcher(
                source=row.code,
                base_url=row.base_url or "",
                rate_limit=replace(self._rate_limit_for_news(row), source=row.code.split("_")[0]),
                limiter=self.limiter,
                timeout_seconds=20.0,
                max_retries=3,
            )
        )
        provider.assert_permitted()
        return provider

    @staticmethod
    def _rate_limit_for_news(row: NewsSource) -> RateLimitConfig:
        """Politeness limits, from the row.

        A news source is somebody's server rather than an open data endpoint
        sized for it, so the floor between requests is deliberately higher than
        the market data one.
        """
        return RateLimitConfig(
            source=row.code,
            requests_per_minute=row.rate_limit_per_minute,
            max_concurrency=1,
            min_interval_ms=1000,
        )

    async def active_news_sources(self, session: AsyncSession) -> list[NewsSource]:
        rows = await session.execute(
            select(NewsSource).where(NewsSource.is_active.is_(True)).order_by(NewsSource.code)
        )
        return list(rows.scalars().all())

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
