from __future__ import annotations

import pytest

from app.core.cache import bump_cache_version, cache_version, versioned_key

pytestmark = pytest.mark.integration


async def test_ping(redis_client):
    assert await redis_client.ping() is True


async def test_set_get_expire(redis_client):
    await redis_client.setex("probe:key", 30, "value")
    assert await redis_client.get("probe:key") == "value"
    assert 0 < await redis_client.ttl("probe:key") <= 30


async def test_cache_version_starts_at_zero(redis_client):
    """Regression guard: an initial version of 1 would make the first
    invalidation a no-op, because Redis INCR on a missing key also yields 1."""
    assert await cache_version(redis_client, "scores") == 0


async def test_first_bump_actually_invalidates(redis_client):
    before = await versioned_key(redis_client, "scores", "2330")
    await bump_cache_version(redis_client, "scores")
    after = await versioned_key(redis_client, "scores", "2330")
    assert before != after


async def test_bumping_version_retires_a_namespace(redis_client):
    old = await versioned_key(redis_client, "scores", "2330")
    await redis_client.setex(old, 60, "91.2")

    await bump_cache_version(redis_client, "scores")
    new = await versioned_key(redis_client, "scores", "2330")

    assert new != old
    assert await redis_client.get(new) is None, "new version must start empty"
    assert await redis_client.get(old) == "91.2", "old keys expire on their own, no SCAN sweep"


async def test_namespaces_are_independent(redis_client):
    await bump_cache_version(redis_client, "scores")
    assert await cache_version(redis_client, "scores") == 1
    assert await cache_version(redis_client, "news") == 0
