"""Redis clients.

Three logical databases on one instance (see ARCHITECTURE.md §7.2 / TD-1):
  db0 cache · db1 broker · db2 results

Cache invalidation uses a *version prefix* rather than key scanning: bumping
`cache_version:<namespace>` retires a whole namespace in O(1) and lets the old
keys expire on their own. `KEYS`/`SCAN` sweeps are forbidden.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis

from app.core.config import Settings

_client: aioredis.Redis | None = None


def get_redis(settings: Settings) -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_cache_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=settings.HEALTH_TIMEOUT_SECONDS,
            socket_timeout=settings.HEALTH_TIMEOUT_SECONDS,
            health_check_interval=30,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def cache_version(redis: aioredis.Redis, namespace: str) -> int:
    """Current version of a namespace.

    A missing counter is version **0**, not 1. This matters: Redis `INCR` on a
    missing key yields 1, so if the default were also 1 the very first
    invalidation would be a no-op and stale entries would survive it.
    """
    raw: Any = await redis.get(f"cache_version:{namespace}")
    return int(raw) if raw is not None else 0


async def bump_cache_version(redis: aioredis.Redis, namespace: str) -> int:
    return int(await redis.incr(f"cache_version:{namespace}"))


async def versioned_key(redis: aioredis.Redis, namespace: str, key: str) -> str:
    version = await cache_version(redis, namespace)
    return f"v{version}:{namespace}:{key}"
