"""008 — news intelligence: aliases, sources, items, entities, relations, events

Six tables, and three of them exist to stop something rather than to store
something.

`news_sources` carries `robots_policy`, `licence_note` and `verified_at`, and a
CHECK constraint refuses to let a source be `is_active` without all three. The
rule lives in the database rather than in the loader because it is the one rule
that has to survive a careless UPDATE: a crawler nobody established was welcome,
running daily from the operator"s own address, is not a defect anybody notices
from the inside.

`news` has a title, a summary and no body. `DATA_SOURCES.md` §8.1 says the full
text is used in the pipeline and not retained; a `body` column would make that a
matter of discipline, and discipline is what fails at 2am on a Tuesday. The
complete payload stays in `raw_ingestions`, which is provenance rather than a
corpus. A second constraint refuses an item published more than a day after it
was ingested — a future-dated article is a parse error, and catching it here
stops it becoming a factor value nobody could have traded on.

`news_stock_relations.evidence` is NOT NULL and cannot be an empty object. The
claim "this story is about this company" is a model output; storing it without
the snippet, the rule and the path makes it a claim nobody can disagree with.
The same table forbids a DIRECT relation with a non-zero hop count, so
propagation through the supply chain can never be laundered into a direct
mention.

`news_entities` allows a confidence of exactly 1 only for `SOURCE_ATTRIBUTED` —
where the exchange itself named the company. A dictionary match and a language
model are both inferences, and an inference of 1.0 is a number nothing
downstream can discount.

Nothing here is a hypertable yet, and `news_embeddings` is not created at all:
the embedding dimension follows from a model choice that has not been made, and
a `vector(1024)` column guessing at BGE-M3 would be a migration to undo rather
than a decision to keep.

Revision ID: 0008_news
Revises: 0007_derived_quant
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_news"
down_revision: str | None = "0007_derived_quant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("provider_type", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=True),
        sa.Column("feed_url", sa.String(length=255), nullable=True),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("credibility", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("robots_policy", sa.String(length=24), nullable=False),
        sa.Column("licence_note", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attributes_symbols", sa.Boolean(), nullable=False),
        sa.Column("publishes_time_of_day", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "NOT is_active OR (robots_policy IN ('NOT_APPLICABLE','VERIFIED_ALLOWED') "
            "AND licence_note IS NOT NULL AND verified_at IS NOT NULL)",
            name=op.f("ck_news_sources_active_requires_verified_permission"),
        ),
        sa.CheckConstraint(
            "robots_policy IN ('NOT_APPLICABLE','VERIFIED_ALLOWED','VERIFIED_DISALLOWED','UNVERIFIED')",
            name=op.f("ck_news_sources_robots_policy_allowed"),
        ),
        sa.CheckConstraint(
            "source_type IN ('OFFICIAL','IR','GOV','MEDIA','AGGREGATOR')",
            name=op.f("ck_news_sources_source_type_allowed"),
        ),
        sa.CheckConstraint(
            "credibility >= 0 AND credibility <= 1", name=op.f("ck_news_sources_credibility_range")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_sources")),
        sa.UniqueConstraint("code", name=op.f("uq_news_sources_code")),
    )
    op.create_table(
        "entity_aliases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("alias", sa.String(length=150), nullable=False),
        sa.Column("alias_normalized", sa.String(length=150), nullable=False),
        sa.Column("alias_type", sa.String(length=20), nullable=False),
        sa.Column("is_ambiguous", sa.Boolean(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("ingestion_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "alias_type IN ('SYMBOL','FULL_NAME','SHORT_NAME','ENGLISH_NAME','ENGLISH_SHORT','DISCOVERED')",
            name=op.f("ck_entity_aliases_alias_type_allowed"),
        ),
        sa.CheckConstraint(
            r"symbol ~ '^[0-9A-Z]{4,10}$'", name=op.f("ck_entity_aliases_symbol_format")
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_id"],
            ["raw_ingestions.id"],
            name=op.f("fk_entity_aliases_ingestion_id_raw_ingestions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_aliases")),
        sa.UniqueConstraint("symbol", "market", "alias_normalized", name="uq_entity_aliases_key"),
    )
    op.create_index(
        op.f("ix_entity_aliases_ingestion_id"), "entity_aliases", ["ingestion_id"], unique=False
    )
    op.create_index(
        "ix_entity_aliases_lookup", "entity_aliases", ["alias_normalized", "approved"], unique=False
    )
    op.create_index(
        "ix_entity_aliases_symbol", "entity_aliases", ["symbol", "market"], unique=False
    )
    op.create_table(
        "news",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_code", sa.String(length=30), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("url_canonical", sa.String(length=1000), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("lang", sa.String(length=2), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at_is_date_only", sa.Boolean(), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("source_category", sa.String(length=60), nullable=True),
        sa.Column("simhash", sa.BigInteger(), nullable=True),
        sa.Column("cluster_id", sa.BigInteger(), nullable=True),
        sa.Column("is_duplicate", sa.Boolean(), nullable=False),
        sa.Column("sentiment", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("sentiment_confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("importance", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("processing_status", sa.String(length=20), nullable=False),
        sa.Column("model_version", sa.String(length=50), nullable=True),
        sa.Column("ingestion_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "processing_status IN ('PENDING','NORMALIZED','LINKED','SCORED','FAILED')",
            name=op.f("ck_news_processing_status_allowed"),
        ),
        sa.CheckConstraint(
            "published_at <= ingested_at + interval '1 day'",
            name=op.f("ck_news_published_before_ingested"),
        ),
        sa.CheckConstraint(
            "importance IS NULL OR (importance >= 0 AND importance <= 1)",
            name=op.f("ck_news_importance_range"),
        ),
        sa.CheckConstraint(
            "sentiment IS NULL OR (sentiment >= -1 AND sentiment <= 1)",
            name=op.f("ck_news_sentiment_range"),
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_id"],
            ["raw_ingestions.id"],
            name=op.f("fk_news_ingestion_id_raw_ingestions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news")),
        sa.UniqueConstraint("url_hash", name="uq_news_url_hash"),
    )
    op.create_index("ix_news_cluster", "news", ["cluster_id"], unique=False)
    op.create_index("ix_news_effective", "news", ["effective_date"], unique=False)
    op.create_index(op.f("ix_news_ingestion_id"), "news", ["ingestion_id"], unique=False)
    op.create_index("ix_news_published", "news", ["published_at"], unique=False)
    op.create_index("ix_news_source", "news", ["source_code", "published_at"], unique=False)
    op.create_table(
        "news_entities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("news_id", sa.BigInteger(), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_text", sa.String(length=150), nullable=False),
        sa.Column("normalized", sa.String(length=150), nullable=True),
        sa.Column("symbol", sa.String(length=10), nullable=True),
        sa.Column("alias_id", sa.BigInteger(), nullable=True),
        sa.Column("extraction_method", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("model_version", sa.String(length=50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence < 1 OR extraction_method = 'SOURCE_ATTRIBUTED'",
            name=op.f("ck_news_entities_certainty_requires_attribution"),
        ),
        sa.CheckConstraint(
            "entity_type IN ('STOCK','PERSON','PRODUCT','TECH','COUNTRY','ORG','POLICY','EVENT')",
            name=op.f("ck_news_entities_entity_type_allowed"),
        ),
        sa.CheckConstraint(
            "extraction_method IN ('DICTIONARY','SOURCE_ATTRIBUTED','LLM','HYBRID')",
            name=op.f("ck_news_entities_extraction_method_allowed"),
        ),
        sa.CheckConstraint(
            "char_start IS NULL OR char_end IS NULL OR char_end > char_start",
            name=op.f("ck_news_entities_offsets_ordered"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_news_entities_confidence_range")
        ),
        sa.ForeignKeyConstraint(
            ["alias_id"],
            ["entity_aliases.id"],
            name=op.f("fk_news_entities_alias_id_entity_aliases"),
        ),
        sa.ForeignKeyConstraint(
            ["news_id"], ["news.id"], name=op.f("fk_news_entities_news_id_news"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_entities")),
    )
    op.create_index("ix_news_entities_news", "news_entities", ["news_id"], unique=False)
    op.create_index("ix_news_entities_symbol", "news_entities", ["symbol"], unique=False)
    op.create_table(
        "news_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("news_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("event_subtype", sa.String(length=40), nullable=True),
        sa.Column("severity", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("classification_method", sa.String(length=20), nullable=False),
        sa.Column("model_version", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_news_events_confidence_range")
        ),
        sa.CheckConstraint(
            "severity IS NULL OR (severity >= 0 AND severity <= 1)",
            name=op.f("ck_news_events_severity_range"),
        ),
        sa.ForeignKeyConstraint(
            ["news_id"], ["news.id"], name=op.f("fk_news_events_news_id_news"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_events")),
        sa.UniqueConstraint("news_id", "event_type", name="uq_news_event"),
    )
    op.create_index("ix_news_events_news", "news_events", ["news_id"], unique=False)
    op.create_index(
        "ix_news_events_type", "news_events", ["event_type", "event_time"], unique=False
    )
    op.create_table(
        "news_stock_relations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("news_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("relation_type", sa.String(length=20), nullable=False),
        sa.Column("hop_count", sa.SmallInteger(), nullable=False),
        sa.Column("impact_score", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("impact_direction", sa.SmallInteger(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("sentiment", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("time_horizon", sa.String(length=10), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_version", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(relation_type = 'DIRECT') = (hop_count = 0) OR relation_type = 'MACRO'",
            name=op.f("ck_news_stock_relations_direct_means_zero_hops"),
        ),
        sa.CheckConstraint(
            "evidence <> '{}'::jsonb", name=op.f("ck_news_stock_relations_evidence_not_empty")
        ),
        sa.CheckConstraint(
            "relation_type IN ('DIRECT','SUPPLIER','CUSTOMER','COMPETITOR','INDUSTRY','THEMATIC','MACRO')",
            name=op.f("ck_news_stock_relations_relation_type_allowed"),
        ),
        sa.CheckConstraint(
            r"symbol ~ '^[0-9A-Z]{4,10}$'", name=op.f("ck_news_stock_relations_symbol_format")
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_news_stock_relations_confidence_range"),
        ),
        sa.CheckConstraint(
            "hop_count >= 0 AND hop_count <= 5", name=op.f("ck_news_stock_relations_hop_range")
        ),
        sa.CheckConstraint(
            "impact_direction IN (-1, 0, 1)", name=op.f("ck_news_stock_relations_direction_allowed")
        ),
        sa.CheckConstraint(
            "impact_score >= 0 AND impact_score <= 1",
            name=op.f("ck_news_stock_relations_impact_range"),
        ),
        sa.ForeignKeyConstraint(
            ["news_id"],
            ["news.id"],
            name=op.f("fk_news_stock_relations_news_id_news"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_stock_relations")),
        sa.UniqueConstraint("news_id", "symbol", "relation_type", name="uq_news_stock_relation"),
    )
    op.create_index(
        "ix_news_relations_impact", "news_stock_relations", ["impact_score"], unique=False
    )
    op.create_index("ix_news_relations_news", "news_stock_relations", ["news_id"], unique=False)
    op.create_index(
        "ix_news_relations_symbol", "news_stock_relations", ["symbol", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_news_relations_symbol", table_name="news_stock_relations")
    op.drop_index("ix_news_relations_news", table_name="news_stock_relations")
    op.drop_index("ix_news_relations_impact", table_name="news_stock_relations")
    op.drop_table("news_stock_relations")
    op.drop_index("ix_news_events_type", table_name="news_events")
    op.drop_index("ix_news_events_news", table_name="news_events")
    op.drop_table("news_events")
    op.drop_index("ix_news_entities_symbol", table_name="news_entities")
    op.drop_index("ix_news_entities_news", table_name="news_entities")
    op.drop_table("news_entities")
    op.drop_index("ix_news_source", table_name="news")
    op.drop_index("ix_news_published", table_name="news")
    op.drop_index(op.f("ix_news_ingestion_id"), table_name="news")
    op.drop_index("ix_news_effective", table_name="news")
    op.drop_index("ix_news_cluster", table_name="news")
    op.drop_table("news")
    op.drop_index("ix_entity_aliases_symbol", table_name="entity_aliases")
    op.drop_index("ix_entity_aliases_lookup", table_name="entity_aliases")
    op.drop_index(op.f("ix_entity_aliases_ingestion_id"), table_name="entity_aliases")
    op.drop_table("entity_aliases")
    op.drop_table("news_sources")
