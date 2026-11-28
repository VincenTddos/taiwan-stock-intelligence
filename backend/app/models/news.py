"""News intelligence schema.

Six tables. The shape of them is driven by three commitments that predate
Phase 4 and that the schema has to make impossible to break rather than merely
discourage.

**No full text.** `news` has a title and a summary and no body column. The
storage policy in `DATA_SOURCES.md` §8.1 says the full text is used in the
processing pipeline and not retained; a `body` column would make that a matter
of discipline. The complete payload stays in `raw_ingestions`, which is
provenance rather than a corpus.

**Knowledge time is not ingestion time, and neither is event time.**
`published_at` is when the market could have known. `ingested_at` is when we
found out. `news_events.event_time` is when the thing happened, which for an
exchange announcement is routinely days earlier. Three columns because they are
three different facts, and a backtest that used the wrong one would look
entirely reasonable.

**Every derived claim carries its evidence and its version.** A row in
`news_stock_relations` says a story is about a company. That is a model output,
so it stores what produced it (`model_version`), how sure it is (`confidence`),
and *why* (`evidence`, NOT NULL) — the snippet, the offsets, the rule, the path
through the graph. A relation nobody can inspect is a relation nobody can
disagree with.

Keyed by `symbol`, not `stock_id`, for the reason given in `app/models/quant.py`:
`stock_master` is a slowly-changing dimension whose `id` identifies a version of
a company's attributes, not the company.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, provenance_column


class AliasType(StrEnum):
    SYMBOL = "SYMBOL"
    FULL_NAME = "FULL_NAME"
    SHORT_NAME = "SHORT_NAME"
    ENGLISH_NAME = "ENGLISH_NAME"
    ENGLISH_SHORT = "ENGLISH_SHORT"
    #: Discovered by a model rather than derived from the security master.
    DISCOVERED = "DISCOVERED"


class EntityType(StrEnum):
    STOCK = "STOCK"
    PERSON = "PERSON"
    PRODUCT = "PRODUCT"
    TECH = "TECH"
    COUNTRY = "COUNTRY"
    ORG = "ORG"
    POLICY = "POLICY"
    EVENT = "EVENT"


class ExtractionMethod(StrEnum):
    #: Exact match against `entity_aliases`. Deterministic and reproducible.
    DICTIONARY = "DICTIONARY"
    #: The source itself attributed the item to a company. Evidence, not
    #: inference — and the only method whose confidence may be 1.
    SOURCE_ATTRIBUTED = "SOURCE_ATTRIBUTED"
    LLM = "LLM"
    HYBRID = "HYBRID"


class RelationType(StrEnum):
    DIRECT = "DIRECT"
    SUPPLIER = "SUPPLIER"
    CUSTOMER = "CUSTOMER"
    COMPETITOR = "COMPETITOR"
    INDUSTRY = "INDUSTRY"
    THEMATIC = "THEMATIC"
    MACRO = "MACRO"


class ProcessingStatus(StrEnum):
    PENDING = "PENDING"
    NORMALIZED = "NORMALIZED"
    LINKED = "LINKED"
    SCORED = "SCORED"
    FAILED = "FAILED"


class TimeHorizon(StrEnum):
    INTRADAY = "INTRADAY"
    SHORT = "SHORT"
    MEDIUM = "MEDIUM"
    LONG = "LONG"


# --------------------------------------------------------------------------
class EntityAlias(Base):
    """The name dictionary: every string that means a listed company.

    Built from the security master rather than typed by hand — `t187ap03_L`
    carries four name columns, so a symbol yields four to six aliases and the
    whole market yields roughly ten thousand entries.

    `is_ambiguous` and `approved` exist because the dictionary is the first
    layer of entity extraction and its mistakes are systematic. 「台積」matches
    inside 「台積電」; a bare four-digit code matches inside a price. An
    ambiguous alias is kept and flagged so the disambiguation rules know to
    demand context, rather than being deleted and losing the recall.

    An alias a model discovered starts unapproved and does not participate in
    matching until a person accepts it. That is the difference between a
    dictionary and a guess that compounds.
    """

    __tablename__ = "entity_aliases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)

    alias: Mapped[str] = mapped_column(String(150), nullable=False)
    alias_normalized: Mapped[str] = mapped_column(String(150), nullable=False)
    alias_type: Mapped[str] = mapped_column(String(20), nullable=False)

    #: Short or common strings that also occur as ordinary text.
    is_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Model-discovered aliases wait for a person.
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    source: Mapped[str] = mapped_column(String(30), nullable=False)
    ingestion_id: Mapped[int | None] = provenance_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("symbol", "market", "alias_normalized", name="uq_entity_aliases_key"),
        CheckConstraint(
            "alias_type IN ('SYMBOL','FULL_NAME','SHORT_NAME','ENGLISH_NAME',"
            "'ENGLISH_SHORT','DISCOVERED')",
            name="alias_type_allowed",
        ),
        CheckConstraint(r"symbol ~ '^[0-9A-Z]{4,10}$'", name="symbol_format"),
        Index("ix_entity_aliases_lookup", "alias_normalized", "approved"),
        Index("ix_entity_aliases_symbol", "symbol", "market"),
    )


class NewsSource(Base):
    """A publisher, and the recorded reason we are allowed to read it.

    `robots_policy`, `licence_note` and `verified_at` are not documentation.
    The registry refuses to build a provider for a source whose policy is
    `UNVERIFIED`, on the same principle as an unverified market data source:
    "it did not object" is not permission.
    """

    __tablename__ = "news_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    code: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(40), nullable=False)

    base_url: Mapped[str | None] = mapped_column(String(255))
    feed_url: Mapped[str | None] = mapped_column(String(255))

    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    credibility: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)

    robots_policy: Mapped[str] = mapped_column(String(24), nullable=False, default="UNVERIFIED")
    licence_note: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Whether the source attributes items to a company itself, and whether it
    #: publishes a time of day. Both change what a downstream claim may assert,
    #: so they are recorded per source rather than assumed per pipeline.
    attributes_symbols: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    publishes_time_of_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('OFFICIAL','IR','GOV','MEDIA','AGGREGATOR')",
            name="source_type_allowed",
        ),
        CheckConstraint(
            "robots_policy IN ('NOT_APPLICABLE','VERIFIED_ALLOWED',"
            "'VERIFIED_DISALLOWED','UNVERIFIED')",
            name="robots_policy_allowed",
        ),
        CheckConstraint("credibility >= 0 AND credibility <= 1", name="credibility_range"),
        # A source cannot be switched on until somebody recorded why reading it
        # is permitted and what may be stored. The constraint is in the database
        # because this is the one rule that must survive a careless UPDATE.
        CheckConstraint(
            "NOT is_active OR (robots_policy IN ('NOT_APPLICABLE','VERIFIED_ALLOWED') "
            "AND licence_note IS NOT NULL AND verified_at IS NOT NULL)",
            name="active_requires_verified_permission",
        ),
    )


class NewsItem(Base):
    """One story. Title, summary, link — never the body."""

    __tablename__ = "news"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    source_code: Mapped[str] = mapped_column(String(30), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255))

    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    url_canonical: Mapped[str] = mapped_column(String(1000), nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(2), nullable=False, default="zh")

    #: Knowledge time.
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: True when the source published only a date. Such an item cannot support
    #: an intraday claim, and the flag is what stops one being made.
    published_at_is_date_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: The trading session this item belongs to for factor purposes. Set by the
    #: pipeline against the calendar, not derived from the timestamp — an item
    #: published after the close belongs to the next session.
    effective_date: Mapped[date | None] = mapped_column(Date)

    source_category: Mapped[str | None] = mapped_column(String(60))

    simhash: Mapped[int | None] = mapped_column(BigInteger)
    cluster_id: Mapped[int | None] = mapped_column(BigInteger)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    sentiment: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    sentiment_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    importance: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))

    processing_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ProcessingStatus.PENDING
    )
    model_version: Mapped[str | None] = mapped_column(String(50))

    ingestion_id: Mapped[int | None] = provenance_column()

    __table_args__ = (
        UniqueConstraint("url_hash", name="uq_news_url_hash"),
        CheckConstraint(
            "processing_status IN ('PENDING','NORMALIZED','LINKED','SCORED','FAILED')",
            name="processing_status_allowed",
        ),
        CheckConstraint(
            "sentiment IS NULL OR (sentiment >= -1 AND sentiment <= 1)", name="sentiment_range"
        ),
        CheckConstraint(
            "importance IS NULL OR (importance >= 0 AND importance <= 1)",
            name="importance_range",
        ),
        # Knowledge time cannot follow the moment we learned it by more than a
        # clock skew. A future-dated article is a parse error, and catching it
        # here stops it from becoming a factor value nobody could have traded.
        CheckConstraint(
            "published_at <= ingested_at + interval '1 day'", name="published_before_ingested"
        ),
        Index("ix_news_published", "published_at"),
        Index("ix_news_source", "source_code", "published_at"),
        Index("ix_news_cluster", "cluster_id"),
        Index("ix_news_effective", "effective_date"),
    )


class NewsEntity(Base):
    """An entity mentioned in a story, with where in the text it was found."""

    __tablename__ = "news_entities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    news_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news.id", ondelete="CASCADE"), nullable=False
    )

    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_text: Mapped[str] = mapped_column(String(150), nullable=False)
    normalized: Mapped[str | None] = mapped_column(String(150))

    symbol: Mapped[str | None] = mapped_column(String(10))
    alias_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entity_aliases.id"))

    extraction_method: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)

    #: Character offsets into the text the extractor saw. The evidence anchor —
    #: without it, "this story mentions 2330" cannot be shown to anyone.
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)

    model_version: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('STOCK','PERSON','PRODUCT','TECH','COUNTRY','ORG','POLICY','EVENT')",
            name="entity_type_allowed",
        ),
        CheckConstraint(
            "extraction_method IN ('DICTIONARY','SOURCE_ATTRIBUTED','LLM','HYBRID')",
            name="extraction_method_allowed",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        # Only the source's own attribution may be certain. A dictionary match
        # and a model are both inferences, and an inference of 1.0 is a claim
        # nothing downstream can discount.
        CheckConstraint(
            "confidence < 1 OR extraction_method = 'SOURCE_ATTRIBUTED'",
            name="certainty_requires_attribution",
        ),
        CheckConstraint(
            "char_start IS NULL OR char_end IS NULL OR char_end > char_start",
            name="offsets_ordered",
        ),
        Index("ix_news_entities_news", "news_id"),
        Index("ix_news_entities_symbol", "symbol"),
    )


class NewsStockRelation(Base):
    """The claim that a story is about a company, and the reason for it."""

    __tablename__ = "news_stock_relations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    news_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)

    relation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    hop_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    impact_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    impact_direction: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    sentiment: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    time_horizon: Mapped[str] = mapped_column(String(10), nullable=False, default=TimeHorizon.SHORT)

    #: Why. Snippets with offsets, the rule that fired, the path through the
    #: supply chain, the components of the score. NOT NULL, because a relation
    #: nobody can inspect is a relation nobody can disagree with.
    evidence: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    model_version: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("news_id", "symbol", "relation_type", name="uq_news_stock_relation"),
        CheckConstraint(
            "relation_type IN ('DIRECT','SUPPLIER','CUSTOMER','COMPETITOR',"
            "'INDUSTRY','THEMATIC','MACRO')",
            name="relation_type_allowed",
        ),
        CheckConstraint("impact_score >= 0 AND impact_score <= 1", name="impact_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("impact_direction IN (-1, 0, 1)", name="direction_allowed"),
        CheckConstraint("hop_count >= 0 AND hop_count <= 5", name="hop_range"),
        # A direct relation is the story naming the company. Anything reached
        # through the graph has travelled at least one edge, and a DIRECT
        # relation with hops would make propagation invisible in the data.
        CheckConstraint(
            "(relation_type = 'DIRECT') = (hop_count = 0) OR relation_type = 'MACRO'",
            name="direct_means_zero_hops",
        ),
        CheckConstraint("evidence <> '{}'::jsonb", name="evidence_not_empty"),
        CheckConstraint(r"symbol ~ '^[0-9A-Z]{4,10}$'", name="symbol_format"),
        Index("ix_news_relations_symbol", "symbol", "created_at"),
        Index("ix_news_relations_news", "news_id"),
        Index("ix_news_relations_impact", "impact_score"),
    )


class NewsEvent(Base):
    """What kind of event a story reports, and when the event happened."""

    __tablename__ = "news_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    news_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("news.id", ondelete="CASCADE"), nullable=False
    )

    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    event_subtype: Mapped[str | None] = mapped_column(String(40))

    severity: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)

    #: When the event occurred, which may precede publication by days. Kept
    #: apart from `published_at` because one is when it happened and the other
    #: is when it could be traded on.
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    classification_method: Mapped[str] = mapped_column(String(20), nullable=False)
    model_version: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("news_id", "event_type", name="uq_news_event"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint(
            "severity IS NULL OR (severity >= 0 AND severity <= 1)", name="severity_range"
        ),
        Index("ix_news_events_news", "news_id"),
        Index("ix_news_events_type", "event_type", "event_time"),
    )


__all__ = [
    "AliasType",
    "EntityAlias",
    "EntityType",
    "ExtractionMethod",
    "NewsEntity",
    "NewsEvent",
    "NewsItem",
    "NewsSource",
    "NewsStockRelation",
    "ProcessingStatus",
    "RelationType",
    "TimeHorizon",
]
