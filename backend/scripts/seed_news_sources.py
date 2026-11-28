"""Seed the news source registry.

Only sources whose permission has actually been established are seeded active.
Everything else belongs here too — as a row with `robots_policy='UNVERIFIED'`
and `is_active=false`, so the candidate list is visible and the work of checking
it is visible with it. A registry containing only the sources someone finished
checking looks like a registry of every source that exists.

Phase 4 starts with two TWSE endpoints and nothing else, deliberately. They are
open government data served over a documented JSON API: no crawler, no robots
question, a licence that permits commercial reuse with attribution, and — for
the material information feed — an exact announcement time and the company's own
code. The media sources in `DATA_SOURCES.md` §8.2 each need their robots.txt and
terms read before they can be switched on, and that is a task with a person in
it rather than a script.

Idempotent: safe to run on every deploy.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import dispose_engine, get_sessionmaker
from app.models.news import NewsSource
from app.providers.news import RobotsPolicy, SourceType
from app.providers.twse_news import GOV_LICENCE, MATERIAL_ENDPOINT, NEWS_ENDPOINT

#: The date the two TWSE endpoints were fetched, parsed and their payloads
#: recorded — the evidence behind `is_active=true`.
VERIFIED_AT = datetime(2026, 8, 20, tzinfo=UTC)

SOURCES: list[dict[str, Any]] = [
    {
        "code": "TWSE_MATERIAL",
        "name": "臺灣證券交易所 上市公司每日重大訊息 (t187ap04_L)",
        "provider_type": "TWSE_MATERIAL",
        "base_url": MATERIAL_ENDPOINT,
        "feed_url": MATERIAL_ENDPOINT,
        "source_type": SourceType.OFFICIAL,
        "credibility": Decimal("1.000"),
        "robots_policy": RobotsPolicy.NOT_APPLICABLE,
        "licence_note": GOV_LICENCE,
        "verified_at": VERIFIED_AT,
        "attributes_symbols": True,
        "publishes_time_of_day": True,
        "is_active": True,
        "rate_limit_per_minute": 60,
        "notes": (
            "The highest-value news source available: it carries the exact "
            "announcement time (發言日期 + 發言時間), the company code, and the date "
            "the underlying event occurred (事實發生日) — three separate clocks, all "
            "real. The only source where a DIRECT stock link is the publisher's own "
            "attribution rather than an inference. Quirks: the subject field is named "
            "'主旨 ' with a trailing space, and 發言時間 is HHMMSS without zero padding."
        ),
    },
    {
        "code": "TWSE_NEWS",
        "name": "臺灣證券交易所 新聞 (news/newsList)",
        "provider_type": "TWSE_NEWS",
        "base_url": NEWS_ENDPOINT,
        "feed_url": NEWS_ENDPOINT,
        "source_type": SourceType.OFFICIAL,
        "credibility": Decimal("1.000"),
        "robots_policy": RobotsPolicy.NOT_APPLICABLE,
        "licence_note": GOV_LICENCE,
        "verified_at": VERIFIED_AT,
        "attributes_symbols": False,
        "publishes_time_of_day": False,
        "is_active": True,
        "rate_limit_per_minute": 60,
        "notes": (
            "Title, link and date only. Date-level publication means every item is "
            "flagged `published_at_is_date_only`: a story published at some point "
            "during a session cannot be lined up against an intraday move, and the "
            "flag is what stops something downstream pretending it can."
        ),
    },
    # --- candidates, deliberately inactive ---------------------------------
    {
        "code": "MOPS_ANNOUNCEMENT",
        "name": "公開資訊觀測站 重大訊息",
        "provider_type": "MOPS_ANNOUNCEMENT",
        "base_url": "https://mops.twse.com.tw",
        "feed_url": None,
        "source_type": SourceType.OFFICIAL,
        "credibility": Decimal("1.000"),
        "robots_policy": RobotsPolicy.UNVERIFIED,
        "licence_note": None,
        "verified_at": None,
        "attributes_symbols": True,
        "publishes_time_of_day": True,
        "is_active": False,
        "rate_limit_per_minute": 20,
        "notes": (
            "Form-POST driven with HTML responses. Covers announcements TWSE's "
            "OpenAPI does not, but is far more fragile and its robots.txt has not "
            "been read. Prefer the OpenAPI equivalents where they exist."
        ),
    },
    {
        "code": "GOV_PRESS",
        "name": "政府部會新聞稿（經濟部、國發會、金管會）",
        "provider_type": "GOV_PRESS",
        "base_url": None,
        "feed_url": None,
        "source_type": SourceType.GOV,
        "credibility": Decimal("0.900"),
        "robots_policy": RobotsPolicy.UNVERIFIED,
        "licence_note": None,
        "verified_at": None,
        "attributes_symbols": False,
        "publishes_time_of_day": False,
        "is_active": False,
        "rate_limit_per_minute": 20,
        "notes": "Policy events. Each department's feed and terms to be checked separately.",
    },
    {
        "code": "MEDIA_RSS",
        "name": "財經媒體公開 RSS（逐一確認條款）",
        "provider_type": "MEDIA_RSS",
        "base_url": None,
        "feed_url": None,
        "source_type": SourceType.MEDIA,
        "credibility": Decimal("0.750"),
        "robots_policy": RobotsPolicy.UNVERIFIED,
        "licence_note": None,
        "verified_at": None,
        "attributes_symbols": False,
        "publishes_time_of_day": True,
        "is_active": False,
        "rate_limit_per_minute": 10,
        "notes": (
            "A placeholder for the candidates in DATA_SOURCES.md §8.2, not a source. "
            "Each outlet needs its own row, its own robots.txt reading and its own "
            "licence note before anything is fetched — and a shared row would let one "
            "outlet's approval stand in for another's."
        ),
    },
]

COLUMNS = tuple(SOURCES[0])


async def seed() -> None:
    async with get_sessionmaker()() as session:
        for row in SOURCES:
            payload = {key: row.get(key) for key in COLUMNS}
            stmt = pg_insert(NewsSource).values(payload)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[NewsSource.code],
                    set_={k: getattr(stmt.excluded, k) for k in COLUMNS if k != "code"},
                )
            )
        await session.commit()

    active = sum(1 for row in SOURCES if row["is_active"])
    print(
        f"seeded {len(SOURCES)} news sources ({active} active, {len(SOURCES) - active} candidate)"
    )


async def main() -> None:
    try:
        await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
