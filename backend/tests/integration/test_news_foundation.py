"""Phase 4 foundations: what a news source must prove before it is read.

Nothing here processes a story. What is being established is the set of things
the pipeline will not be able to do later, because the schema, the contracts and
the registry refuse them now:

* fetch from a source nobody established we may read
* store an article's body
* claim a story is about a company without recording why
* let an inference carry the certainty of a publisher's own attribution
* mistake the time an event happened for the time the market could know

Each of those is cheap to enforce today and very expensive to retrofit once a
pipeline is producing rows.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import AppEnv, get_settings
from app.models.news import (
    EntityAlias,
    NewsEntity,
    NewsEvent,
    NewsItem,
    NewsSource,
    NewsStockRelation,
)
from app.models.ops import Transport
from app.providers.base import ProviderError, ProviderErrorKind
from app.providers.news import DEFAULT_CREDIBILITY, NewsProvider, RobotsPolicy, SourceType
from app.providers.registry import NEWS_PROVIDER_TYPES, ProviderRegistry
from app.providers.twse_news import (
    TWSEMaterialInformationProvider,
    TWSENewsProvider,
    parse_material_information,
    parse_news_list,
)
from app.schemas.contracts import (
    EvidenceSnippet,
    ExtractionMethod,
    NewsEntityMention,
    NewsEventTag,
    NewsStockLink,
    Provenance,
    RelationType,
)

pytestmark = pytest.mark.integration

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "twse"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _provenance() -> Provenance:
    return Provenance(
        model_version="news-rules-v0",
        dataset_version="d1",
        feature_version="f1",
        calculated_at=datetime.now(UTC),
        data_as_of=datetime.now(UTC).date(),
    )


@pytest.fixture
async def seeded_news_sources(session):
    """Two verified sources and one that nobody has checked."""
    rows = [
        NewsSource(
            code="TWSE_MATERIAL",
            name="material information",
            provider_type="TWSE_MATERIAL",
            base_url="https://openapi.twse.com.tw/v1",
            source_type=SourceType.OFFICIAL,
            credibility=Decimal("1.000"),
            robots_policy=RobotsPolicy.NOT_APPLICABLE,
            licence_note="Open Government Data License, Taiwan v1.0",
            verified_at=datetime(2026, 8, 20, tzinfo=UTC),
            attributes_symbols=True,
            publishes_time_of_day=True,
            is_active=True,
        ),
        NewsSource(
            code="TWSE_NEWS",
            name="exchange news",
            provider_type="TWSE_NEWS",
            base_url="https://openapi.twse.com.tw/v1",
            source_type=SourceType.OFFICIAL,
            credibility=Decimal("1.000"),
            robots_policy=RobotsPolicy.NOT_APPLICABLE,
            licence_note="Open Government Data License, Taiwan v1.0",
            verified_at=datetime(2026, 8, 20, tzinfo=UTC),
            is_active=True,
        ),
        NewsSource(
            code="SOME_OUTLET",
            name="a media site nobody has checked",
            provider_type="TWSE_NEWS",
            base_url="https://example.invalid",
            source_type=SourceType.MEDIA,
            credibility=Decimal("0.750"),
            robots_policy=RobotsPolicy.UNVERIFIED,
            is_active=False,
        ),
    ]
    for row in rows:
        session.add(row)
    await session.commit()
    return rows


# ============================================================ permission
class TestPermissionIsAPositiveFact:
    async def test_the_database_refuses_to_activate_an_unverified_source(self, session):
        """The rule lives in a CHECK constraint, not in the loader.

        A crawler nobody established was welcome, running daily from the
        operator's own address, is not a defect anyone notices from the inside —
        so the last line of defence is the one an UPDATE cannot walk past.
        """
        session.add(
            NewsSource(
                code="CARELESS",
                name="switched on without checking",
                provider_type="TWSE_NEWS",
                source_type=SourceType.MEDIA,
                credibility=Decimal("0.750"),
                robots_policy=RobotsPolicy.UNVERIFIED,
                is_active=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_activation_needs_a_licence_note_as_well_as_a_robots_policy(self, session):
        """Two different questions: may we fetch it, and may we store it."""
        session.add(
            NewsSource(
                code="NO_LICENCE",
                name="robots fine, terms unread",
                provider_type="TWSE_NEWS",
                source_type=SourceType.MEDIA,
                credibility=Decimal("0.750"),
                robots_policy=RobotsPolicy.VERIFIED_ALLOWED,
                licence_note=None,
                verified_at=datetime.now(UTC),
                is_active=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_the_registry_refuses_an_inactive_source(
        self, session, registry, seeded_news_sources
    ):
        with pytest.raises(ProviderError) as exc:
            await registry.get_news(session, "SOME_OUTLET")
        assert exc.value.kind is ProviderErrorKind.CONFIG_ERROR
        assert "not active" in str(exc.value)

    async def test_a_provider_refuses_itself_when_nobody_checked(self):
        """The class checks too, not only the row.

        The row records what was decided; the class records what the code will
        actually fetch. A provider pointed at a path nobody checked should fail
        on its own account rather than inherit another row's approval.
        """

        class UncheckedProvider(NewsProvider):
            code = "UNCHECKED"
            robots_policy = RobotsPolicy.UNVERIFIED

            async def fetch(self, *, since=None):  # type: ignore[no-untyped-def]
                raise NotImplementedError

            async def health(self):  # type: ignore[no-untyped-def]
                raise NotImplementedError

        with pytest.raises(ProviderError, match="not permission"):
            UncheckedProvider().assert_permitted()

    async def test_a_disallowed_robots_policy_is_refused(self):
        class ForbiddenProvider(NewsProvider):
            code = "FORBIDDEN"
            robots_policy = RobotsPolicy.VERIFIED_DISALLOWED
            licence_note = "irrelevant — we may not fetch it"

            async def fetch(self, *, since=None):  # type: ignore[no-untyped-def]
                raise NotImplementedError

            async def health(self):  # type: ignore[no-untyped-def]
                raise NotImplementedError

        with pytest.raises(ProviderError, match="forbids"):
            ForbiddenProvider().assert_permitted()

    async def test_permission_without_a_licence_note_is_still_refused(self):
        class NoLicence(NewsProvider):
            code = "NO_LICENCE"
            robots_policy = RobotsPolicy.NOT_APPLICABLE
            licence_note = ""

            async def fetch(self, *, since=None):  # type: ignore[no-untyped-def]
                raise NotImplementedError

            async def health(self):  # type: ignore[no-untyped-def]
                raise NotImplementedError

        with pytest.raises(ProviderError, match="licence"):
            NoLicence().assert_permitted()

    def test_credibility_defaults_follow_the_publisher_type(self):
        assert DEFAULT_CREDIBILITY[SourceType.OFFICIAL] == Decimal("1.00")
        assert DEFAULT_CREDIBILITY[SourceType.MEDIA] < DEFAULT_CREDIBILITY[SourceType.GOV]
        assert DEFAULT_CREDIBILITY[SourceType.AGGREGATOR] < DEFAULT_CREDIBILITY[SourceType.MEDIA]


# ================================================================ parsers
class TestMaterialInformationParser:
    def test_the_announcement_time_survives_as_an_instant(self):
        """`發言時間` is HHMMSS with no zero padding.

        `65903` is 06:59:03. Parsed as an integer of hours it is 65903 o'clock,
        and parsed as a date-only item it loses the very thing that makes this
        source worth having.
        """
        records, errors = parse_material_information(_fixture("t187ap04_L_1150819.json"))
        assert records and not errors

        first = records[0]
        assert first["published_at"].tzinfo is not None
        assert not first["published_at_is_date_only"]
        # 06:59:03 Taipei is 22:59:03 UTC the previous day.
        assert first["published_at"] == datetime(2026, 8, 17, 22, 59, 3, tzinfo=UTC)

    def test_the_event_time_is_kept_apart_from_the_announcement(self):
        """Two clocks, and they are days apart in the recording.

        A study of what the market did on the news uses the publication time; a
        study of what the company did uses the event time. Collapsing them makes
        one of those studies quietly wrong.
        """
        records, _ = parse_material_information(_fixture("t187ap04_L_1150819.json"))
        with_event = [r for r in records if r["event_time"] is not None]
        assert with_event, "the recording no longer carries 事實發生日"
        assert any(r["event_time"] < r["published_at"] for r in with_event)

    def test_the_company_attributes_itself(self):
        records, _ = parse_material_information(_fixture("t187ap04_L_1150819.json"))
        assert all(r["attributed_symbol"] for r in records)
        assert all(r["attributed_symbol"].isalnum() for r in records)

    def test_the_subject_field_has_a_trailing_space_and_it_is_handled(self):
        payload = _fixture("t187ap04_L_1150819.json")
        assert any("主旨 " in row for row in payload), (
            "the exchange stopped padding the field name; the parser accepts both "
            "spellings, so this is a note rather than a break"
        )
        records, _ = parse_material_information(payload)
        assert all(r["title"] for r in records)

    def test_only_a_summary_is_kept(self):
        """The full announcement is in the payload; the record keeps a slice."""
        payload = _fixture("t187ap04_L_1150819.json")
        records, _ = parse_material_information(payload)
        longest = max(len(r["summary"] or "") for r in records)
        assert longest <= 400
        assert any(len(str(row.get("說明", ""))) > 400 for row in payload), (
            "the recording no longer contains an announcement long enough to truncate"
        )

    def test_a_changed_payload_shape_is_reported_not_guessed(self):
        with pytest.raises(ProviderError) as exc:
            parse_material_information({"unexpected": "envelope"})
        assert exc.value.kind is ProviderErrorKind.SCHEMA_CHANGED

    def test_the_generated_id_is_stable(self):
        """Re-fetching the same window must produce the same ids.

        The endpoint has no item id. A random or time-based one would make every
        poll look like new news.
        """
        first, _ = parse_material_information(_fixture("t187ap04_L_1150819.json"))
        second, _ = parse_material_information(_fixture("t187ap04_L_1150819.json"))
        assert [r["external_id"] for r in first] == [r["external_id"] for r in second]


class TestNewsListParser:
    def test_every_item_is_flagged_date_only(self):
        """This feed publishes a date and no time.

        The flag is what stops something downstream lining a story up against an
        intraday move it cannot possibly be aligned with.
        """
        records, errors = parse_news_list(_fixture("news_list_1150819.json"))
        assert records and not errors
        assert all(r["published_at_is_date_only"] for r in records)
        assert all(r["attributed_symbol"] is None for r in records)

    def test_the_url_is_the_identity(self):
        records, _ = parse_news_list(_fixture("news_list_1150819.json"))
        assert all(r["external_id"] for r in records)
        assert len({r["external_id"] for r in records}) == len({r["url"] for r in records})


# ============================================================== transport
class TestReplayedFetching:
    async def test_a_replayed_run_does_not_reach_the_exchange(
        self, session, registry, seeded_news_sources
    ):
        provider = await registry.get_news(session, "TWSE_MATERIAL")
        result = await provider.fetch()
        assert result.metadata.transport is Transport.REPLAY
        assert result.records
        await provider.aclose()

    async def test_since_filters_without_asking_the_source_again(
        self, session, registry, seeded_news_sources
    ):
        """Most feeds serve a fixed window and cannot be asked for more.

        `since` is therefore advisory and applied to what arrived, which is why
        it is documented as advisory rather than looking like a query parameter
        the source honours.
        """
        provider = await registry.get_news(session, "TWSE_MATERIAL")
        everything = await provider.fetch()
        latest = max(r["published_at"] for r in everything.records)

        narrowed = await provider.fetch(since=latest)
        assert 0 < len(narrowed.records) <= len(everything.records)
        assert all(r["published_at"] >= latest for r in narrowed.records)
        await provider.aclose()

    async def test_both_registered_providers_declare_what_they_can_see(self):
        assert set(NEWS_PROVIDER_TYPES) == {"TWSE_MATERIAL", "TWSE_NEWS"}
        assert TWSEMaterialInformationProvider.attributes_symbols
        assert TWSEMaterialInformationProvider.publishes_time_of_day
        # The weaker source says so, rather than being described by its absence.
        assert not TWSENewsProvider.attributes_symbols
        assert not TWSENewsProvider.publishes_time_of_day

    async def test_replay_is_refused_in_production(self, session, seeded_news_sources):
        """Recorded data is genuine and old; serving it from production would
        misrepresent freshness, so the mode is refused rather than warned about.

        `AppEnv.PRODUCTION`, not the string: `model_copy` skips validation, so a
        raw string would leave the setting as a `str` and the check comparing
        against the enum would pass the test by not firing at all.
        """
        settings = get_settings().model_copy(update={"APP_ENV": AppEnv.PRODUCTION})
        with pytest.raises(ProviderError, match="not permitted in production"):
            await ProviderRegistry(settings).get_news(session, "TWSE_MATERIAL")


# =============================================================== contracts
class TestContractsRefuseTheDangerousShapes:
    def test_an_inference_may_not_be_certain(self):
        """Only a publisher's own attribution may carry confidence 1.

        A dictionary hit on 「台積」 is strong and still an inference — the string
        occurs inside other names and beside prices. A confidence of exactly 1
        is a number nothing downstream can discount.
        """
        with pytest.raises(ValueError, match="SOURCE_ATTRIBUTED"):
            NewsEntityMention(
                entity_type="STOCK",
                entity_text="台積電",
                symbol="2330",
                extraction_method=ExtractionMethod.DICTIONARY,
                confidence=Decimal(1),
                evidence=EvidenceSnippet(text="台積電法說會", char_start=0, char_end=3),
            )

        # The same value is fine when the exchange said it.
        mention = NewsEntityMention(
            entity_type="STOCK",
            entity_text="2330",
            symbol="2330",
            extraction_method=ExtractionMethod.SOURCE_ATTRIBUTED,
            confidence=Decimal(1),
        )
        assert mention.confidence == 1

    def test_an_inferred_mention_must_carry_its_evidence(self):
        with pytest.raises(ValueError, match="evidence"):
            NewsEntityMention(
                entity_type="STOCK",
                entity_text="台積電",
                symbol="2330",
                extraction_method=ExtractionMethod.LLM,
                confidence=Decimal("0.8"),
            )

    def test_a_stock_link_cannot_be_made_without_evidence(self):
        with pytest.raises(ValueError):
            NewsStockLink(
                symbol="2330",
                relation_type=RelationType.DIRECT,
                impact_score=Decimal("0.8"),
                impact_direction=1,
                confidence=Decimal("0.9"),
                evidence=[],
                rule="dictionary_match",
                provenance=_provenance(),
            )

    def test_direct_means_zero_hops_in_both_directions(self):
        """A propagated link stored as DIRECT looks exactly like a mention.

        The difference is the whole basis for discounting the second-hop claim,
        so the type refuses to let it be lost.
        """
        with pytest.raises(ValueError, match="DIRECT means"):
            NewsStockLink(
                symbol="2330",
                relation_type=RelationType.DIRECT,
                hop_count=2,
                impact_score=Decimal("0.5"),
                impact_direction=1,
                confidence=Decimal("0.6"),
                evidence=[EvidenceSnippet(text="NVDA 擴大訂單")],
                path=["NVDA", "CoWoS", "2330"],
                rule="supply_chain_hop",
                provenance=_provenance(),
            )

        with pytest.raises(ValueError, match="DIRECT means"):
            NewsStockLink(
                symbol="2330",
                relation_type=RelationType.SUPPLIER,
                hop_count=0,
                impact_score=Decimal("0.5"),
                impact_direction=1,
                confidence=Decimal("0.6"),
                evidence=[EvidenceSnippet(text="NVDA 擴大訂單")],
                rule="supply_chain_hop",
                provenance=_provenance(),
            )

    def test_a_propagated_link_must_record_the_path_it_travelled(self):
        with pytest.raises(ValueError, match="path"):
            NewsStockLink(
                symbol="2330",
                relation_type=RelationType.SUPPLIER,
                hop_count=1,
                impact_score=Decimal("0.5"),
                impact_direction=1,
                confidence=Decimal("0.6"),
                evidence=[EvidenceSnippet(text="NVDA 擴大訂單")],
                rule="supply_chain_hop",
                provenance=_provenance(),
            )

    def test_a_valid_propagated_link_is_accepted(self):
        link = NewsStockLink(
            symbol="2330",
            relation_type=RelationType.SUPPLIER,
            hop_count=1,
            impact_score=Decimal("0.42"),
            impact_direction=1,
            confidence=Decimal("0.61"),
            evidence=[EvidenceSnippet(text="NVDA 擴大訂單", char_start=10, char_end=20)],
            path=["NVDA", "2330"],
            rule="supply_chain_hop",
            provenance=_provenance(),
        )
        assert link.hop_count == 1
        assert link.provenance.model_version

    def test_evidence_offsets_must_be_ordered(self):
        with pytest.raises(ValueError, match="char_end"):
            EvidenceSnippet(text="台積電", char_start=10, char_end=5)

    def test_an_event_tag_keeps_the_event_time_separate(self):
        tag = NewsEventTag(
            event_type="ORDER_WIN",
            confidence=Decimal("0.7"),
            event_time=datetime(2026, 8, 17, tzinfo=UTC),
            classification_method=ExtractionMethod.DICTIONARY,
            provenance=_provenance(),
        )
        assert tag.event_time < datetime.now(UTC)


# ================================================================= schema
class TestSchemaInvariants:
    async def test_there_is_nowhere_to_put_an_article_body(self):
        """The storage policy is a missing column, not a convention.

        `DATA_SOURCES.md` §8.1 says the full text is used in the pipeline and
        not retained. A `body` column would make that a matter of discipline,
        and discipline is what fails at 2am on a Tuesday.
        """
        columns = set(NewsItem.__table__.columns.keys())
        assert "summary" in columns
        for forbidden in ("body", "content", "full_text", "article_text", "html"):
            assert forbidden not in columns

    async def test_an_item_published_after_it_was_ingested_is_refused(self, session):
        session.add(
            NewsItem(
                source_code="TWSE_NEWS",
                url="https://example.invalid/a",
                url_canonical="https://example.invalid/a",
                url_hash="a" * 64,
                title="published next month",
                published_at=datetime.now(UTC) + timedelta(days=30),
                ingested_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_a_relation_cannot_be_stored_with_empty_evidence(self, session):
        item = NewsItem(
            source_code="TWSE_MATERIAL",
            url="https://example.invalid/b",
            url_canonical="https://example.invalid/b",
            url_hash="b" * 64,
            title="an announcement",
            published_at=datetime.now(UTC),
        )
        session.add(item)
        await session.flush()

        session.add(
            NewsStockRelation(
                news_id=item.id,
                symbol="2330",
                market="TWSE",
                relation_type="DIRECT",
                hop_count=0,
                impact_score=Decimal("0.5"),
                impact_direction=1,
                confidence=Decimal("0.9"),
                evidence={},
                model_version="v0",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_a_direct_relation_cannot_carry_hops(self, session):
        item = NewsItem(
            source_code="TWSE_MATERIAL",
            url="https://example.invalid/c",
            url_canonical="https://example.invalid/c",
            url_hash="c" * 64,
            title="an announcement",
            published_at=datetime.now(UTC),
        )
        session.add(item)
        await session.flush()

        session.add(
            NewsStockRelation(
                news_id=item.id,
                symbol="2330",
                market="TWSE",
                relation_type="DIRECT",
                hop_count=2,
                impact_score=Decimal("0.5"),
                impact_direction=1,
                confidence=Decimal("0.9"),
                evidence={"rule": "laundered"},
                model_version="v0",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_only_a_source_attribution_may_be_certain_in_the_database_too(self, session):
        """The same rule as the contract, enforced a second time.

        The contract stops the pipeline writing it; the constraint stops
        everything else — a fixture, a migration, a hand-written INSERT during
        an incident.
        """
        item = NewsItem(
            source_code="TWSE_NEWS",
            url="https://example.invalid/d",
            url_canonical="https://example.invalid/d",
            url_hash="d" * 64,
            title="a story",
            published_at=datetime.now(UTC),
        )
        session.add(item)
        await session.flush()

        session.add(
            NewsEntity(
                news_id=item.id,
                entity_type="STOCK",
                entity_text="台積電",
                symbol="2330",
                extraction_method="DICTIONARY",
                confidence=Decimal(1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_an_ambiguous_alias_is_kept_and_flagged_rather_than_dropped(self, session):
        """Recall matters as much as precision for the dictionary.

        「台積」 matches inside 「台積電」 and is still the string people write.
        Deleting it loses the mention; flagging it tells the disambiguation
        rules to demand context.
        """
        session.add_all(
            [
                EntityAlias(
                    symbol="2330",
                    market="TWSE",
                    alias="台積電",
                    alias_normalized="台積電",
                    alias_type="SHORT_NAME",
                    source="TWSE",
                ),
                EntityAlias(
                    symbol="2330",
                    market="TWSE",
                    alias="台積",
                    alias_normalized="台積",
                    alias_type="SHORT_NAME",
                    is_ambiguous=True,
                    source="TWSE",
                ),
            ]
        )
        await session.commit()

        rows = (
            (await session.execute(select(EntityAlias).where(EntityAlias.symbol == "2330")))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert any(r.is_ambiguous for r in rows)

    async def test_a_discovered_alias_waits_for_a_person(self, session):
        session.add(
            EntityAlias(
                symbol="2330",
                market="TWSE",
                alias="TSMC Arizona",
                alias_normalized="tsmc arizona",
                alias_type="DISCOVERED",
                approved=False,
                source="LLM",
            )
        )
        await session.commit()

        row = (
            await session.execute(select(EntityAlias).where(EntityAlias.alias_type == "DISCOVERED"))
        ).scalar_one()
        assert not row.approved, (
            "a model-discovered alias that matched immediately would compound its "
            "own mistakes into the dictionary"
        )

    async def test_an_event_records_how_it_was_classified(self, session):
        item = NewsItem(
            source_code="TWSE_MATERIAL",
            url="https://example.invalid/e",
            url_canonical="https://example.invalid/e",
            url_hash="e" * 64,
            title="訂單",
            published_at=datetime.now(UTC),
        )
        session.add(item)
        await session.flush()

        session.add(
            NewsEvent(
                news_id=item.id,
                event_type="ORDER_WIN",
                confidence=Decimal("0.8"),
                event_time=datetime.now(UTC) - timedelta(days=2),
                classification_method="DICTIONARY",
                model_version="news-rules-v0",
            )
        )
        await session.commit()

        row = (await session.execute(select(NewsEvent))).scalar_one()
        assert row.model_version
        assert row.classification_method == "DICTIONARY"
        assert row.event_time < row.created_at


# ================================================ the factor contract holds
class TestNewsFactorsDeclareTheirDependency:
    def test_every_news_factor_names_what_will_supply_it(self):
        """The catalogue has to say who owes the input, not just that one is missing.

        "missing news_sentiment_7d" tells a reader what is absent; "Phase 4 news
        sentiment analysis" tells them when to expect it, which is the question
        that actually gets asked.
        """
        from app.quant import factors as fct
        from app.quant.factors.spec import FactorCategory

        news_like = [
            spec
            for spec in fct.REGISTRY
            if spec.category in (FactorCategory.SENTIMENT, FactorCategory.NEWS)
        ]
        assert len(news_like) == 7
        assert all(spec.provided_by.startswith("Phase 4") for spec in news_like)
        assert all(spec.inputs for spec in news_like)

    def test_no_factor_becomes_production_eligible_by_default(self):
        """Only a PASS verdict is production-eligible.

        The permissive guess — treating an unmeasured factor as usable — is both
        the easy one to write and invisible in the result, so it is stated on the
        verdict rather than left to each consumer.
        """
        from app.quant.factors.validation import Verdict

        assert Verdict.PASS.production_eligible
        assert not Verdict.RESEARCH_ONLY.production_eligible
        assert not Verdict.INSUFFICIENT_DATA.production_eligible
        assert not Verdict.NOT_COMPUTABLE.production_eligible
