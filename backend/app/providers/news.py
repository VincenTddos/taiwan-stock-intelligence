"""News provider abstraction.

Separate from both `BaseMarketDataProvider` and `CorporateActionProvider`, for
the same reason those are separate from each other: the source that publishes a
price is not the source that publishes a story, and binding them would make one
decision permanent.

What is different about news, and what this module exists to encode:

**Permission is a positive fact, not an absence of objection.** A price endpoint
is open government data and needs no argument. A news source needs one — a feed
the publisher offers, an exchange announcement that is itself public
information, a robots policy someone actually read. `DATA_SOURCES.md` §8.1 lists
what is allowed and what is not, and a provider here must state which case it is
in before it can be constructed. `RobotsPolicy.UNVERIFIED` is refused, exactly
as an unverified market data source is.

**The full text is not ours to keep.** Providers return a title, a summary and a
link. There is no body field on the contract, so "we only store a summary" is
not a convention anyone has to remember — a provider that wanted to keep the
article would have nowhere to put it. Where a source is open government data and
copyright does not apply, the storage-cost argument still does, and consistency
beats a special case.

**Knowledge time is the publication time, and the two clocks are separate.**
`published_at` is when the market could have known. `ingested_at` is when we
found out. An exchange announcement also carries the time the *event* occurred,
which may be days earlier — three clocks, none interchangeable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, NotRequired, TypedDict

from app.models.ops import Transport
from app.providers.base import (
    Capability,
    ProviderError,
    ProviderErrorKind,
    ProviderHealth,
    ProviderResult,
)
from app.providers.http import JsonFetcher


class SourceType(StrEnum):
    """What kind of publisher this is. Drives the default credibility."""

    #: An exchange or regulator publishing its own announcements. The text *is*
    #: the public record.
    OFFICIAL = "OFFICIAL"
    #: A company's own investor relations release.
    IR = "IR"
    #: A government department's press release.
    GOV = "GOV"
    #: A media organisation's own reporting.
    MEDIA = "MEDIA"
    #: A syndicator or aggregator republishing others' work.
    AGGREGATOR = "AGGREGATOR"


class RobotsPolicy(StrEnum):
    """Why fetching from this source is permitted."""

    #: No crawling involved — a documented API or open data feed.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: robots.txt was read and permits the paths this provider fetches.
    VERIFIED_ALLOWED = "VERIFIED_ALLOWED"
    #: robots.txt was read and forbids them. The provider must not be built.
    VERIFIED_DISALLOWED = "VERIFIED_DISALLOWED"
    #: Nobody has looked. Refused — the same reason an unverified market data
    #: source is refused rather than tried.
    UNVERIFIED = "UNVERIFIED"


#: Credibility defaults by publisher type, from `DATA_SOURCES.md` §8.3. A row in
#: `news_sources` may override its own; these are what a new source starts at,
#: so nobody has to invent a number.
DEFAULT_CREDIBILITY: dict[SourceType, Decimal] = {
    SourceType.OFFICIAL: Decimal("1.00"),
    SourceType.IR: Decimal("0.95"),
    SourceType.GOV: Decimal("0.90"),
    SourceType.MEDIA: Decimal("0.75"),
    SourceType.AGGREGATOR: Decimal("0.60"),
}


class NewsRecord(TypedDict):
    """The canonical shape a news provider must produce.

    Deliberately without a body. See the module docstring: the absence is the
    policy.
    """

    source_code: str
    url: str
    title: str

    #: Knowledge time — when the market could have known. Never `now()`, never
    #: the fetch time, and never inferred from a date alone when the source
    #: publishes a time.
    published_at: datetime

    external_id: NotRequired[str | None]
    summary: NotRequired[str | None]
    lang: NotRequired[str]

    #: When the underlying event happened, where the source says so. An exchange
    #: announcement routinely reports something that occurred days earlier, and
    #: collapsing the two would make an event look like news.
    event_time: NotRequired[datetime | None]

    #: A stock the *source itself* attributes the item to. Only official
    #: announcements have this; for everything else the link is inferred later
    #: and carries a confidence. A source-attributed symbol is evidence, not a
    #: guess, and is kept apart from inferred ones for that reason.
    attributed_symbol: NotRequired[str | None]

    #: Whether the source published a time of day, or only a date. A date-only
    #: item cannot be used for an intraday study and must not pretend it can.
    published_at_is_date_only: NotRequired[bool]

    #: The regulation clause or category the source filed the item under, where
    #: it has one. Raw, unmapped — classification is a later, versioned step.
    source_category: NotRequired[str | None]

    raw: NotRequired[dict[str, Any]]


class NewsProvider(ABC):
    """Contract every news source implements."""

    code: str = "BASE"
    market: str = ""
    transport: Transport = Transport.LIVE

    source_type: SourceType = SourceType.MEDIA
    robots_policy: RobotsPolicy = RobotsPolicy.UNVERIFIED
    licence_note: str = ""

    #: True when the source attributes items to a stock itself. Drives whether
    #: a `DIRECT` relation is evidence or inference.
    attributes_symbols: bool = False
    #: True when the source publishes a time of day rather than only a date.
    publishes_time_of_day: bool = False

    capabilities: frozenset[Capability] = frozenset()

    def __init__(self, *, fetcher: JsonFetcher | None = None) -> None:
        """`fetcher` is the transport seam, as it is for corporate actions.

        Replay substitutes here so a recorded response runs through the real
        parser rather than a stand-in.
        """
        self._fetcher = fetcher

    @property
    def fetcher(self) -> JsonFetcher:
        if self._fetcher is None:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"{self.code} was constructed without a transport",
                source=self.code,
            )
        return self._fetcher

    @property
    def credibility(self) -> Decimal:
        return DEFAULT_CREDIBILITY[self.source_type]

    def assert_permitted(self) -> None:
        """Refuse to run without a recorded reason that fetching is allowed.

        Called by the registry before a provider is handed out. The check is
        cheap and the failure it prevents is not: a crawler that nobody
        established was welcome, running daily, from a machine with the
        operator's IP on it.
        """
        if self.robots_policy is RobotsPolicy.UNVERIFIED:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"{self.code}: nobody has checked whether fetching this source is "
                f"permitted. Read its robots.txt and terms, record the finding, "
                f"then enable it — 'it did not object' is not permission.",
                source=self.code,
            )
        if self.robots_policy is RobotsPolicy.VERIFIED_DISALLOWED:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"{self.code}: robots.txt forbids the paths this provider fetches",
                source=self.code,
            )
        if not self.licence_note:
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"{self.code}: no licence note recorded. What may be stored and "
                f"redisplayed has to be written down before anything is stored.",
                source=self.code,
            )

    # ------------------------------------------------------------------
    @abstractmethod
    async def fetch(self, *, since: datetime | None = None) -> ProviderResult:
        """Items published at or after `since`, newest first where the source allows.

        `since` is advisory: most feeds serve a fixed window and cannot be asked
        for more. A provider filters what it can and returns what it has; the
        deduplicator upstream is what makes re-fetching the same window cheap.

        Items that cannot be parsed belong in `ProviderResult.parse_errors`,
        never dropped — a feed changing its shape must look like a failure, not
        like a quiet day with no news.
        """

    @abstractmethod
    async def health(self) -> ProviderHealth:
        """Whether this source is reachable and answering."""

    async def aclose(self) -> None:
        return None


__all__ = [
    "DEFAULT_CREDIBILITY",
    "NewsProvider",
    "NewsRecord",
    "RobotsPolicy",
    "SourceType",
]
