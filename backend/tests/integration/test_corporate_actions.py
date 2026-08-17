"""Corporate action provider abstraction and the adjustment precondition.

Two things are asserted here, and neither is about arithmetic:

* nothing in the domain or service layer can reach a named exchange endpoint —
  a corporate action provider is resolved from the registry or not at all
* a price series cannot be adjusted over a range whose corporate action
  coverage has not been established

The second is the one that matters. An unadjusted ex-dividend gap does not look
like a bug; it looks like a 3% down day, and it will be read as momentum.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.models.market import CorporateActionType
from app.providers.base import ProviderError, ProviderErrorKind, ProviderResult
from app.providers.corporate_actions import (
    EXPECTED_FIELDS,
    CorporateActionProvider,
)
from app.providers.registry import CORPORATE_ACTION_PROVIDER_TYPES, ProviderRegistry
from app.services.corporate_action_service import (
    PRICE_AFFECTING,
    AdjustmentNotPermitted,
    CorporateActionCoverageService,
)

pytestmark = pytest.mark.integration

RANGE = (date(2024, 1, 1), date(2026, 8, 1))


# ============================================================ abstraction
class TestProviderAbstraction:
    def test_every_price_affecting_type_declares_its_expected_fields(self):
        """A type the adjustment pipeline must handle, with no declared fields,
        would let a parser return an empty record and have it accepted."""
        for action in PRICE_AFFECTING:
            assert EXPECTED_FIELDS.get(action), f"{action} has no expected fields"

    def test_capital_reduction_requires_a_ratio_but_not_cash(self):
        """現金減資 returns cash and cancels shares; 彌補虧損減資 only cancels
        shares. The share ratio is what both have in common, so it is the part
        that is required."""
        expected = EXPECTED_FIELDS[CorporateActionType.CAPITAL_REDUCTION]
        assert "split_ratio" in expected
        assert "cash_returned_per_share" not in expected

    async def test_no_implementation_is_an_error_not_a_fallback(self, session, seeded_sources):
        """Until Phase 3 registers one, asking for a corporate action provider
        must fail loudly. Silently handing back a price provider would produce
        a source that answers every question with nothing."""
        registry = ProviderRegistry()
        with pytest.raises(ProviderError) as exc:
            await registry.get_corporate_actions(session, "TWSE")
        assert exc.value.kind is ProviderErrorKind.NOT_SUPPORTED

    async def test_unknown_source_is_rejected_before_anything_else(self, session, seeded_sources):
        registry = ProviderRegistry()
        with pytest.raises(ProviderError) as exc:
            await registry.get_corporate_actions(session, "NOT_A_SOURCE")
        assert exc.value.kind is ProviderErrorKind.CONFIG_ERROR

    async def test_a_registered_implementation_is_resolved_by_configuration(
        self, session, seeded_sources
    ):
        """The registry is the only thing that knows which source implements
        corporate actions. Registering a fake here proves the wiring works
        without any endpoint appearing in domain or service code."""

        class FakeProvider(CorporateActionProvider):
            code = "FAKE"
            supported_actions = PRICE_AFFECTING

            async def get_corporate_actions(self, *, start, end, symbol=None):
                raise NotImplementedError

            async def health(self):
                raise NotImplementedError

        CORPORATE_ACTION_PROVIDER_TYPES["TWSE"] = FakeProvider
        try:
            provider = await ProviderRegistry().get_corporate_actions(session, "TWSE")
            assert isinstance(provider, FakeProvider)
            assert provider.supports(CorporateActionType.CASH_DIVIDEND)
        finally:
            CORPORATE_ACTION_PROVIDER_TYPES.pop("TWSE", None)

    def test_a_provider_reports_only_what_it_can_see(self):
        """`supported_actions` is what stops "no rights issue found" and "this
        source has never carried rights issues" from being the same answer."""

        class DividendsOnly(CorporateActionProvider):
            code = "DIVS"
            supported_actions = frozenset({CorporateActionType.CASH_DIVIDEND})

            async def get_corporate_actions(self, *, start, end, symbol=None):
                return ProviderResult(records=[], metadata=None)  # type: ignore[arg-type]

            async def health(self):
                raise NotImplementedError

        p = DividendsOnly()
        assert p.supports(CorporateActionType.CASH_DIVIDEND)
        assert not p.supports(CorporateActionType.RIGHTS_ISSUE)
        with pytest.raises(ProviderError) as exc:
            p._require(CorporateActionType.RIGHTS_ISSUE)
        assert exc.value.kind is ProviderErrorKind.NOT_SUPPORTED


# ================================================ adjustment precondition
class TestAdjustmentPrecondition:
    async def test_adjustment_is_refused_when_nothing_was_ever_fetched(self, session):
        svc = CorporateActionCoverageService(session)
        with pytest.raises(AdjustmentNotPermitted, match="no corporate action coverage"):
            await svc.assert_adjustable(["2330"], *RANGE)

    async def test_an_empty_result_is_not_the_same_as_no_actions(self, session):
        """The distinction the coverage table exists for. A symbol with genuinely
        zero actions is adjustable once someone has looked; a symbol nobody has
        looked at is not — and both have an empty `corporate_actions`."""
        svc = CorporateActionCoverageService(session)
        await svc.record(
            symbol="1101",
            market="TWSE",
            source="TEST",
            covered_from=RANGE[0],
            covered_to=RANGE[1],
            action_types=PRICE_AFFECTING,
            actions_found=0,
        )
        await session.commit()

        await svc.assert_adjustable(["1101"], *RANGE)  # looked, found none — fine
        with pytest.raises(AdjustmentNotPermitted):
            await svc.assert_adjustable(["1102"], *RANGE)  # never looked

    async def test_partial_range_coverage_is_refused(self, session):
        svc = CorporateActionCoverageService(session)
        await svc.record(
            symbol="2330",
            market="TWSE",
            source="TEST",
            covered_from=date(2025, 1, 1),
            covered_to=RANGE[1],
            action_types=PRICE_AFFECTING,
            actions_found=3,
        )
        await session.commit()

        await svc.assert_adjustable(["2330"], date(2025, 6, 1), RANGE[1])
        with pytest.raises(AdjustmentNotPermitted, match="does not span"):
            await svc.assert_adjustable(["2330"], *RANGE)

    async def test_a_source_that_cannot_see_an_action_type_leaves_a_gap(self, session):
        """Full date coverage from a dividends-only source is still not enough to
        adjust: a capital reduction in that window would be missed entirely."""
        svc = CorporateActionCoverageService(session)
        await svc.record(
            symbol="2603",
            market="TWSE",
            source="DIVIDENDS_ONLY",
            covered_from=RANGE[0],
            covered_to=RANGE[1],
            action_types=frozenset({CorporateActionType.CASH_DIVIDEND}),
            actions_found=4,
        )
        await session.commit()

        with pytest.raises(AdjustmentNotPermitted, match="never searched for"):
            await svc.assert_adjustable(["2603"], *RANGE)

        gaps = await svc.gaps(["2603"], *RANGE)
        assert "CAPITAL_REDUCTION" in gaps[0].missing_action_types
        assert "CASH_DIVIDEND" not in gaps[0].missing_action_types

    async def test_two_partial_sources_can_together_be_complete(self, session):
        svc = CorporateActionCoverageService(session)
        await svc.record(
            symbol="2882",
            market="TWSE",
            source="DIVS",
            covered_from=RANGE[0],
            covered_to=RANGE[1],
            action_types=frozenset(
                {CorporateActionType.CASH_DIVIDEND, CorporateActionType.STOCK_DIVIDEND}
            ),
            actions_found=6,
        )
        await svc.record(
            symbol="2882",
            market="TWSE",
            source="STRUCTURAL",
            covered_from=RANGE[0],
            covered_to=RANGE[1],
            action_types=PRICE_AFFECTING - {CorporateActionType.CASH_DIVIDEND},
            actions_found=1,
        )
        await session.commit()
        await svc.assert_adjustable(["2882"], *RANGE)

    async def test_recording_a_later_range_does_not_erase_an_earlier_one(self, session):
        """A backfill advancing through history must accumulate coverage. If each
        run replaced the last, the range would shrink to whatever ran most
        recently and older history would silently stop being adjustable."""
        svc = CorporateActionCoverageService(session)
        await svc.record(
            symbol="1216",
            market="TWSE",
            source="TEST",
            covered_from=date(2019, 1, 1),
            covered_to=date(2024, 1, 1),
            action_types=PRICE_AFFECTING,
            actions_found=10,
        )
        await session.commit()
        await svc.record(
            symbol="1216",
            market="TWSE",
            source="TEST",
            covered_from=date(2024, 1, 1),
            covered_to=date(2026, 8, 1),
            action_types=PRICE_AFFECTING,
            actions_found=4,
        )
        await session.commit()

        rows = await svc.coverage_for("1216")
        assert len(rows) == 1, "same symbol and source must be one widening row"
        assert rows[0].covered_from == date(2019, 1, 1)
        assert rows[0].covered_to == date(2026, 8, 1)
        await svc.assert_adjustable(["1216"], date(2019, 6, 1), date(2026, 7, 1))

    async def test_the_error_names_every_blocked_symbol(self, session):
        svc = CorporateActionCoverageService(session)
        with pytest.raises(AdjustmentNotPermitted) as exc:
            await svc.assert_adjustable(["1101", "1102", "2330"], *RANGE)
        message = str(exc.value)
        assert "3 of 3 symbols" in message
        for symbol in ("1101", "1102", "2330"):
            assert symbol in message

    async def test_par_value_change_is_not_required_for_adjustment(self, session):
        """It changes the stated par value without changing what a holder owns,
        so it does not enter the factor and must not block adjustment."""
        assert CorporateActionType.PAR_VALUE_CHANGE not in PRICE_AFFECTING
        svc = CorporateActionCoverageService(session)
        await svc.record(
            symbol="2412",
            market="TWSE",
            source="TEST",
            covered_from=RANGE[0],
            covered_to=RANGE[1],
            action_types=PRICE_AFFECTING,
            actions_found=0,
        )
        await session.commit()
        await svc.assert_adjustable(["2412"], *RANGE)


class TestCoverageIsRecordedHonestly:
    async def test_verified_at_is_stored_and_timezone_aware(self, session):
        svc = CorporateActionCoverageService(session)
        before = datetime.now(UTC)
        await svc.record(
            symbol="3008",
            market="TWSE",
            source="TEST",
            covered_from=RANGE[0],
            covered_to=RANGE[1],
            action_types=PRICE_AFFECTING,
            actions_found=2,
        )
        await session.commit()
        row = (await svc.coverage_for("3008"))[0]
        assert row.verified_at is not None
        assert row.verified_at.tzinfo is not None
        assert row.verified_at >= before
        assert row.actions_found == 2
