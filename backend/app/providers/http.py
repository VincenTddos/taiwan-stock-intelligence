"""HTTP transport shared by every live provider.

Bundles the four things every outbound call needs — timeout, retry, rate limit,
structured logging — plus provenance capture, so a provider implementation
contains parsing logic and nothing else.

The retry policy only retries what can plausibly succeed on a second attempt:
timeouts, connection failures, 429 and 5xx. A 404 or a parse failure is not
retried, because asking again will produce the same answer while consuming
quota.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.logging import get_logger
from app.models.ops import Transport
from app.providers.base import ProviderError, ProviderErrorKind, SourceMetadata
from app.providers.rate_limiter import RateLimitConfig, RateLimiter, RateLimitExceeded

log = get_logger(__name__)

# Identifies us honestly. Sources are entitled to know who is calling.
USER_AGENT = "twquant/0.2 (+https://github.com/twquant; research; contact via repo)"

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpFetcher:
    """One instance per source, holding that source's client and limits."""

    def __init__(
        self,
        *,
        source: str,
        base_url: str,
        rate_limit: RateLimitConfig,
        limiter: RateLimiter,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.source = source
        self.base_url = base_url.rstrip("/")
        self.rate_limit = rate_limit
        self.limiter = limiter
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=timeout_seconds, write=10.0, pool=5.0),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                **(headers or {}),
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    async def fetch_json(
        self, path: str, *, dataset: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, SourceMetadata]:
        """GET a JSON document, returning it alongside full provenance."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        request_at = datetime.now(UTC)
        last_error: ProviderError | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                async with self.limiter.slot(self.rate_limit):
                    started = asyncio.get_running_loop().time()
                    response = await self._client.get(url, params=params)
                    duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)

                if response.status_code in RETRY_STATUS:
                    raise ProviderError(
                        ProviderErrorKind.RATE_LIMITED
                        if response.status_code == 429
                        else ProviderErrorKind.UPSTREAM_ERROR,
                        f"HTTP {response.status_code}",
                        source=self.source,
                        endpoint=url,
                        status_code=response.status_code,
                    )
                if response.status_code == 404:
                    raise ProviderError(
                        ProviderErrorKind.NOT_FOUND,
                        "endpoint returned 404",
                        source=self.source,
                        endpoint=url,
                        status_code=404,
                    )
                if response.status_code >= 400:
                    raise ProviderError(
                        ProviderErrorKind.UPSTREAM_ERROR,
                        f"HTTP {response.status_code}",
                        source=self.source,
                        endpoint=url,
                        status_code=response.status_code,
                    )

                body = response.content
                try:
                    payload = response.json()
                except ValueError as exc:
                    # HTML where JSON was expected almost always means the source
                    # changed or is serving an error page.
                    raise ProviderError(
                        ProviderErrorKind.SCHEMA_CHANGED,
                        f"expected JSON, got {response.headers.get('content-type')}",
                        source=self.source,
                        endpoint=url,
                        cause=exc,
                    ) from exc

                metadata = SourceMetadata(
                    source=self.source,
                    source_endpoint=url,
                    dataset=dataset,
                    transport=Transport.LIVE,
                    source_request_at=request_at,
                    source_response_at=datetime.now(UTC),
                    params=dict(params or {}),
                    http_status=response.status_code,
                    duration_ms=duration_ms,
                    response_bytes=len(body),
                    response_hash=hashlib.sha256(body).hexdigest(),
                )
                log.info(
                    "provider_fetch",
                    source=self.source,
                    dataset=dataset,
                    endpoint=url,
                    status=response.status_code,
                    duration_ms=duration_ms,
                    bytes=len(body),
                    attempt=attempt,
                )
                return payload, metadata

            except RateLimitExceeded as exc:
                raise ProviderError(
                    ProviderErrorKind.RATE_LIMITED, str(exc), source=self.source, endpoint=url
                ) from exc
            except httpx.TimeoutException as exc:
                last_error = ProviderError(
                    ProviderErrorKind.TIMEOUT,
                    str(exc),
                    source=self.source,
                    endpoint=url,
                    cause=exc,
                )
            except httpx.HTTPError as exc:
                last_error = ProviderError(
                    ProviderErrorKind.CONNECTION,
                    str(exc),
                    source=self.source,
                    endpoint=url,
                    cause=exc,
                )
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable:
                    raise

            if attempt < self.max_retries:
                # Exponential backoff with jitter: without jitter, N workers that
                # failed together retry together and re-create the same spike.
                delay = min(2 ** (attempt - 1), 8) * (0.5 + random.random())  # noqa: S311
                log.warning(
                    "provider_retry",
                    source=self.source,
                    dataset=dataset,
                    attempt=attempt,
                    max_retries=self.max_retries,
                    delay_s=round(delay, 2),
                    error=str(last_error),
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        log.error(
            "provider_fetch_failed",
            source=self.source,
            dataset=dataset,
            endpoint=url,
            attempts=self.max_retries,
            kind=str(last_error.kind),
        )
        raise last_error

    async def probe(self, path: str) -> tuple[bool, float | None, str | None]:
        """Cheap reachability check. Never raises."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        started = asyncio.get_running_loop().time()
        try:
            resp = await self._client.head(url)
            if resp.status_code >= 400:
                resp = await self._client.get(url)
            latency = (asyncio.get_running_loop().time() - started) * 1000
            return resp.status_code < 400, round(latency, 2), None
        except Exception as exc:
            latency = (asyncio.get_running_loop().time() - started) * 1000
            return False, round(latency, 2), str(exc)[:200]


__all__ = ["RETRY_STATUS", "USER_AGENT", "HttpFetcher"]
