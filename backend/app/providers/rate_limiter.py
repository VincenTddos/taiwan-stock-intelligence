"""Central rate limiting.

No provider sleeps on its own. Every outbound request passes through this
limiter, which enforces three independent constraints per source:

* **requests per minute** — a sliding window, not a fixed bucket, so a burst at
  59s followed by another at 61s cannot double the intended rate
* **requests per day** — a hard daily ceiling for sources that publish one
* **concurrency** — how many requests may be in flight at once

Backed by Redis so the limit holds across the API process, every Celery worker,
and a backfill running in parallel with the daily job. A per-process limiter
would let N workers each use the full quota, which is how an IP gets blocked.

Falls back to an in-process limiter when Redis is unavailable, because
rate-limiting locally is strictly better than not rate-limiting at all.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import redis.asyncio as aioredis

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    source: str
    requests_per_minute: int = 60
    requests_per_day: int | None = None
    max_concurrency: int = 2
    min_interval_ms: int = 0

    @property
    def interval_seconds(self) -> float:
        return self.min_interval_ms / 1000.0


class RateLimitExceeded(Exception):
    """Raised only when a daily cap is hit — that one cannot be waited out."""

    def __init__(self, source: str, limit: int) -> None:
        self.source = source
        self.limit = limit
        super().__init__(f"{source}: daily request limit of {limit} reached")


# Lua keeps check-and-increment atomic. Doing it in two round trips lets N
# workers each observe "under the limit" simultaneously and all proceed.
_SLIDING_WINDOW_LUA = """
local key    = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window)
local used = redis.call('ZCARD', key)
if used < limit then
    redis.call('ZADD', key, now_ms, member)
    redis.call('PEXPIRE', key, window)
    return {1, limit - used - 1, 0}
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry_ms = window - (now_ms - tonumber(oldest[2]))
return {0, 0, retry_ms}
"""


class RateLimiter:
    """Shared limiter. One instance per process; state lives in Redis."""

    def __init__(self, redis: aioredis.Redis | None = None) -> None:
        self._redis = redis
        self._script: Any | None = None
        # Monotonic counter for sorted-set members. Two requests inside the same
        # millisecond would otherwise produce the same member, and ZADD would
        # overwrite rather than add — silently letting the limit be exceeded.
        self._seq = 0
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._local_windows: dict[str, deque[float]] = defaultdict(deque)
        self._local_day: dict[str, tuple[str, int]] = {}
        self._last_request: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    def _semaphore(self, cfg: RateLimitConfig) -> asyncio.Semaphore:
        sem = self._semaphores.get(cfg.source)
        if sem is None:
            sem = asyncio.Semaphore(cfg.max_concurrency)
            self._semaphores[cfg.source] = sem
        return sem

    async def _acquire_minute(self, cfg: RateLimitConfig) -> None:
        """Block until a slot in the per-minute window is free."""
        while True:
            wait_ms = await self._try_minute(cfg)
            if wait_ms <= 0:
                return
            # Cap the sleep so a clock skew cannot park a worker for minutes.
            await asyncio.sleep(min(wait_ms / 1000.0, 5.0))

    async def _try_minute(self, cfg: RateLimitConfig) -> float:
        if self._redis is not None:
            try:
                if self._script is None:
                    self._script = self._redis.register_script(_SLIDING_WINDOW_LUA)
                now_ms = int(time.time() * 1000)
                self._seq += 1
                member = f"{now_ms}-{id(self)}-{self._seq}"
                allowed, _remaining, retry_ms = await self._script(
                    keys=[f"ratelimit:{cfg.source}:minute"],
                    args=[now_ms, 60_000, cfg.requests_per_minute, member],
                )
                return 0.0 if int(allowed) == 1 else float(retry_ms)
            except Exception as exc:
                log.warning("ratelimit_redis_unavailable", source=cfg.source, error=str(exc))
                self._redis = None

        # In-process fallback.
        async with self._lock:
            now = time.monotonic()
            window = self._local_windows[cfg.source]
            while window and now - window[0] > 60.0:
                window.popleft()
            if len(window) < cfg.requests_per_minute:
                window.append(now)
                return 0.0
            return max(0.0, 60.0 - (now - window[0])) * 1000.0

    async def _check_day(self, cfg: RateLimitConfig) -> None:
        if cfg.requests_per_day is None:
            return
        today = time.strftime("%Y%m%d", time.gmtime())

        if self._redis is not None:
            try:
                key = f"ratelimit:{cfg.source}:day:{today}"
                used = await self._redis.incr(key)
                if used == 1:
                    await self._redis.expire(key, 172_800)
                if used > cfg.requests_per_day:
                    raise RateLimitExceeded(cfg.source, cfg.requests_per_day)
                return
            except RateLimitExceeded:
                raise
            except Exception as exc:
                log.warning("ratelimit_day_redis_unavailable", source=cfg.source, error=str(exc))
                self._redis = None

        day, used = self._local_day.get(cfg.source, (today, 0))
        if day != today:
            day, used = today, 0
        used += 1
        self._local_day[cfg.source] = (day, used)
        if used > cfg.requests_per_day:
            raise RateLimitExceeded(cfg.source, cfg.requests_per_day)

    async def _respect_min_interval(self, cfg: RateLimitConfig) -> None:
        if cfg.min_interval_ms <= 0:
            return
        async with self._lock:
            last = self._last_request.get(cfg.source)
            now = time.monotonic()
            if last is not None:
                gap = now - last
                if gap < cfg.interval_seconds:
                    await asyncio.sleep(cfg.interval_seconds - gap)
            self._last_request[cfg.source] = time.monotonic()

    # ------------------------------------------------------------------
    def slot(self, cfg: RateLimitConfig) -> _Slot:
        """`async with limiter.slot(cfg): ...` around every outbound request."""
        return _Slot(self, cfg)

    async def stats(self, cfg: RateLimitConfig) -> dict[str, object]:
        """Current usage, for the Data Operations dashboard."""
        minute_used: int | None = None
        day_used: int | None = None
        if self._redis is not None:
            try:
                now_ms = int(time.time() * 1000)
                key = f"ratelimit:{cfg.source}:minute"
                await self._redis.zremrangebyscore(key, 0, now_ms - 60_000)
                minute_used = int(await self._redis.zcard(key))
                today = time.strftime("%Y%m%d", time.gmtime())
                raw = await self._redis.get(f"ratelimit:{cfg.source}:day:{today}")
                day_used = int(raw) if raw else 0
            except Exception:
                log.debug("ratelimit_stats_unavailable", source=cfg.source)
        else:
            minute_used = len(self._local_windows.get(cfg.source, ()))
            day_used = self._local_day.get(cfg.source, ("", 0))[1]

        return {
            "source": cfg.source,
            "requests_per_minute": cfg.requests_per_minute,
            "minute_used": minute_used,
            "requests_per_day": cfg.requests_per_day,
            "day_used": day_used,
            "max_concurrency": cfg.max_concurrency,
        }


class _Slot:
    __slots__ = ("_cfg", "_limiter", "_sem")

    def __init__(self, limiter: RateLimiter, cfg: RateLimitConfig) -> None:
        self._limiter = limiter
        self._cfg = cfg
        self._sem = limiter._semaphore(cfg)

    async def __aenter__(self) -> None:
        await self._sem.acquire()
        try:
            await self._limiter._check_day(self._cfg)
            await self._limiter._acquire_minute(self._cfg)
            await self._limiter._respect_min_interval(self._cfg)
        except BaseException:
            self._sem.release()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._sem.release()


_default: RateLimiter | None = None


def get_rate_limiter(redis: aioredis.Redis | None = None) -> RateLimiter:
    global _default
    if _default is None:
        _default = RateLimiter(redis)
    elif redis is not None and _default._redis is None:
        _default._redis = redis
    return _default


def reset_rate_limiter() -> None:
    """Test hook."""
    global _default
    _default = None


__all__ = [
    "RateLimitConfig",
    "RateLimitExceeded",
    "RateLimiter",
    "get_rate_limiter",
    "reset_rate_limiter",
]
