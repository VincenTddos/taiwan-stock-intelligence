"""TWSE news providers.

Two endpoints, both open government data, both JSON, neither involving a
crawler — which is why Phase 4 starts here rather than at a media RSS feed. The
compliance question for these is settled before it is asked.

    /opendata/t187ap04_L   上市公司每日重大訊息
    /news/newsList         證交所新聞

`t187ap04_L` is the more valuable of the two by a wide margin, and it is worth
saying why:

* it carries the **exact announcement time** (`發言日期` + `發言時間`), so
  knowledge time is a fact rather than a date-level approximation
* it carries the **company code**, so the stock link is the source's own
  attribution rather than something a dictionary matcher inferred
* it carries the **date the event occurred** (`事實發生日`), which is routinely
  earlier than the announcement — a third clock, and a real one

That combination makes it the highest-credibility news source available and the
only one where a `DIRECT` relation is evidence rather than inference.

Three quirks the recorded payload forced, all of them the source's:

* the subject field is named `主旨 ` — **with a trailing space**
* `發言時間` is `HHMMSS` with no zero padding, so `65903` is 06:59:03 and
  parsing it as an integer of hours gives 65903 o'clock
* `說明` holds the full announcement text. It is stored only as a truncated
  summary: copyright does not apply to open government data, but the storage
  argument in `DATA_SOURCES.md` §8.1 does, and a uniform rule beats a special
  case. The complete text remains in `raw_ingestions`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, time, timedelta
from typing import Any

from app.core.logging import get_logger
from app.models.ops import Transport
from app.providers.base import (
    Capability,
    ProviderError,
    ProviderErrorKind,
    ProviderHealth,
    ProviderResult,
)
from app.providers.http import HttpFetcher, JsonFetcher
from app.providers.news import NewsProvider, RobotsPolicy, SourceType
from app.providers.normalize import ParseError, clean, parse_roc_date, parse_symbol
from app.providers.rate_limiter import RateLimitConfig, RateLimiter, get_rate_limiter

log = get_logger(__name__)

MARKET = "TWSE"
OPENAPI = "https://openapi.twse.com.tw/v1"

MATERIAL_ENDPOINT = f"{OPENAPI}/opendata/t187ap04_L"
NEWS_ENDPOINT = f"{OPENAPI}/news/newsList"

#: Taipei is UTC+8 and does not observe daylight saving. Published times are
#: local; every timestamp stored is UTC, because a naive local timestamp is a
#: bug waiting for the first person who compares it with a price bar.
TAIPEI = timedelta(hours=8)

GOV_LICENCE = (
    "政府資料開放授權條款第1版 (Open Government Data License, Taiwan v1.0) — "
    "may be reproduced and adapted, including commercially, with attribution"
)

#: How much of an announcement body is kept. Enough to be useful in a list and
#: to feed a rule-based classifier; far short of republication.
SUMMARY_LIMIT = 400


def _taipei_to_utc(day: object, hhmmss: object, *, field: str) -> tuple[datetime, bool]:
    """Combine a ROC date and an `HHMMSS` string into a UTC instant.

    Returns the instant and whether the time was missing — in which case the
    date's midnight is used and the caller is told, so a date-only item cannot
    silently pass for an intraday one.
    """
    parsed_day = parse_roc_date(day, field=field)
    text = clean(hhmmss)
    if not text or not text.isdigit():
        return (
            datetime.combine(parsed_day, time.min, tzinfo=UTC) - TAIPEI,
            True,
        )

    padded = text.zfill(6)
    hour, minute, second = int(padded[0:2]), int(padded[2:4]), int(padded[4:6])
    if hour > 23 or minute > 59 or second > 59:
        raise ParseError(field, text, "not a time of day")
    local = datetime.combine(parsed_day, time(hour, minute, second), tzinfo=UTC)
    return local - TAIPEI, False


def _summarise(body: object, limit: int = SUMMARY_LIMIT) -> str | None:
    text = clean(body)
    if not text:
        return None
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _url_for_material(symbol: str, announced: datetime) -> str:
    """A stable, resolvable link for an announcement.

    MOPS serves these by company and date; the endpoint itself returns no URL.
    Fabricating a per-item permalink it does not publish would be inventing
    provenance, so this points at the company's announcement listing — which is
    where a reader would land anyway — and the exact item is identified by
    `external_id`.
    """
    return (
        f"https://mops.twse.com.tw/mops/web/t05st01?colorchg=1&co_id={symbol}&year={announced.year}"
    )


def parse_material_information(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """上市公司每日重大訊息 → news records with an exact knowledge time."""
    if not isinstance(payload, list):
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            f"t187ap04_L: expected a list, got {type(payload).__name__}",
            source=MARKET,
        )

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for row in payload:
        try:
            # The trailing space is the exchange's, not a typo here. Both
            # spellings are accepted so a silent fix upstream does not break
            # ingestion on the day it happens.
            subject = clean(row.get("主旨 ") or row.get("主旨"))
            if not subject:
                errors.append({"row": row, "error": "no subject"})
                continue

            symbol = parse_symbol(row["公司代號"], field="公司代號")
            published_at, date_only = _taipei_to_utc(
                row["發言日期"], row.get("發言時間"), field="發言日期"
            )

            event_time: datetime | None = None
            if fact_day := clean(row.get("事實發生日")):
                event_time = (
                    datetime.combine(
                        parse_roc_date(fact_day, field="事實發生日"), time.min, tzinfo=UTC
                    )
                    - TAIPEI
                )

            url = _url_for_material(symbol, published_at)
            # The endpoint has no item id, so one is derived from the fields
            # that identify an announcement. Deterministic, so re-fetching the
            # same window produces the same id and the deduplicator recognises
            # it rather than storing it twice.
            external_id = hashlib.sha256(
                f"{symbol}|{published_at.isoformat()}|{subject}".encode()
            ).hexdigest()[:32]

            records.append(
                {
                    "source_code": "TWSE_MATERIAL",
                    "external_id": external_id,
                    "url": url,
                    "title": subject,
                    "summary": _summarise(row.get("說明")),
                    "lang": "zh",
                    "published_at": published_at,
                    "published_at_is_date_only": date_only,
                    "event_time": event_time,
                    "attributed_symbol": symbol,
                    "source_category": clean(row.get("符合條款")),
                    "raw": dict(row),
                }
            )
        except (ParseError, KeyError, TypeError, ValueError) as exc:
            errors.append({"row": row, "error": str(exc)})

    return records, errors


def parse_news_list(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """證交所新聞 → news records, date-only and unattributed.

    Title, link and date, and nothing else. Every item is flagged
    `published_at_is_date_only`, because a story published at some point on a
    trading day cannot be lined up against an intraday move and must not look
    as if it can.
    """
    if not isinstance(payload, list):
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            f"newsList: expected a list, got {type(payload).__name__}",
            source=MARKET,
        )

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for row in payload:
        try:
            title = clean(row.get("Title"))
            url = clean(row.get("Url"))
            if not title or not url:
                errors.append({"row": row, "error": "missing title or url"})
                continue

            published_at, date_only = _taipei_to_utc(row["Date"], None, field="Date")
            records.append(
                {
                    "source_code": "TWSE_NEWS",
                    "external_id": hashlib.sha256(url.encode()).hexdigest()[:32],
                    "url": url,
                    "title": title,
                    "summary": None,
                    "lang": "zh",
                    "published_at": published_at,
                    "published_at_is_date_only": date_only,
                    "event_time": None,
                    "attributed_symbol": None,
                    "source_category": None,
                    "raw": dict(row),
                }
            )
        except (ParseError, KeyError, TypeError, ValueError) as exc:
            errors.append({"row": row, "error": str(exc)})

    return records, errors


class _TWSENewsBase(NewsProvider):
    dataset = "news"
    market = MARKET
    transport = Transport.LIVE
    source_type = SourceType.OFFICIAL
    # No crawling: a documented open-data endpoint, which is why these two are
    # the sources Phase 4 starts with.
    robots_policy = RobotsPolicy.NOT_APPLICABLE
    licence_note = GOV_LICENCE
    capabilities = frozenset({Capability.NEWS})
    endpoint = MATERIAL_ENDPOINT

    def __init__(
        self,
        *,
        limiter: RateLimiter | None = None,
        rate_limit: RateLimitConfig | None = None,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        fetcher: JsonFetcher | None = None,
    ) -> None:
        cfg = rate_limit or RateLimitConfig(
            source=MARKET, requests_per_minute=60, max_concurrency=2, min_interval_ms=350
        )
        super().__init__(
            fetcher=fetcher
            or HttpFetcher(
                # One budget per exchange, as the corporate action providers do:
                # news polling runs alongside daily ingestion and must not
                # double the rate the exchange sees.
                source=self.code,
                base_url=OPENAPI,
                rate_limit=cfg,
                limiter=limiter or get_rate_limiter(),
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            )
        )

    async def health(self) -> ProviderHealth:
        started = datetime.now(UTC)
        try:
            await self.fetcher.fetch_json(self.endpoint, dataset=self.dataset)
        except ProviderError as exc:
            return ProviderHealth(
                source=self.code, reachable=False, error=str(exc), checked_at=started
            )
        return ProviderHealth(
            source=self.code,
            reachable=True,
            checked_at=started,
            latency_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
        )

    async def aclose(self) -> None:
        await self.fetcher.aclose()

    @staticmethod
    def _since(records: list[dict[str, Any]], since: datetime | None) -> list[dict[str, Any]]:
        if since is None:
            return records
        return [r for r in records if r["published_at"] >= since]


class TWSEMaterialInformationProvider(_TWSENewsBase):
    """上市公司每日重大訊息 — exact times, company-attributed."""

    code = "TWSE_MATERIAL"
    endpoint = MATERIAL_ENDPOINT
    attributes_symbols = True
    publishes_time_of_day = True

    async def fetch(self, *, since: datetime | None = None) -> ProviderResult:
        payload, meta = await self.fetcher.fetch_json(self.endpoint, dataset=self.dataset)
        records, errors = parse_material_information(payload)
        meta.dataset = self.dataset
        meta.source_endpoint = self.endpoint
        kept = self._since(records, since)
        meta.data_as_of = max((r["published_at"].date() for r in kept), default=None)
        return ProviderResult(kept, meta, errors, raw_payload=payload)


class TWSENewsProvider(_TWSENewsBase):
    """證交所新聞 — date-only, unattributed."""

    code = "TWSE_NEWS"
    endpoint = NEWS_ENDPOINT
    attributes_symbols = False
    publishes_time_of_day = False

    async def fetch(self, *, since: datetime | None = None) -> ProviderResult:
        payload, meta = await self.fetcher.fetch_json(self.endpoint, dataset=self.dataset)
        records, errors = parse_news_list(payload)
        meta.dataset = self.dataset
        meta.source_endpoint = self.endpoint
        kept = self._since(records, since)
        meta.data_as_of = max((r["published_at"].date() for r in kept), default=None)
        return ProviderResult(kept, meta, errors, raw_payload=payload)


__all__ = [
    "GOV_LICENCE",
    "MATERIAL_ENDPOINT",
    "NEWS_ENDPOINT",
    "SUMMARY_LIMIT",
    "TWSEMaterialInformationProvider",
    "TWSENewsProvider",
    "parse_material_information",
    "parse_news_list",
]
