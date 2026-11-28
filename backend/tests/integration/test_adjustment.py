"""Adjustment factors, and the series they rewrite.

The arithmetic tests here are deliberately arithmetic: an adjustment is one of
the few things in this system with a published right answer, and TWSE prints it
next to every event as 除權息參考價. So the tests state the exchange's formula
and check against it, and the strongest of them runs on a recorded TWSE payload
and reconciles what this code computes against what the exchange itself
published for the same event.

The other half is about refusal. An adjustment that cannot be computed must
leave the series alone and say so, because the failure mode being defended
against is not a visibly broken number — it is a plausible one.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.models.market import CorporateAction, CorporateActionType, DailyPrice
from app.providers.twse_corporate_actions import parse_results
from app.services.adjustment_service import (
    RECONCILIATION_TOLERANCE_BPS,
    AdjustmentService,
)
from app.services.corporate_action_service import (
    PRICE_AFFECTING,
    AdjustmentNotPermitted,
    CorporateActionCoverageService,
)

pytestmark = pytest.mark.integration

SYMBOL = "9999"
MARKET = "TWSE"
SOURCE = "TWSE_EXRIGHT_RESULTS"
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "twse"


def _action(
    action_type: CorporateActionType,
    *,
    ex_date: date = date(2026, 8, 13),
    symbol: str = SYMBOL,
    announced: datetime | None = None,
    id_: int = 1,
    **fields: object,
) -> CorporateAction:
    """An unpersisted action. `compute_day_factor` is pure over these."""
    return CorporateAction(
        id=id_,
        symbol=symbol,
        market=MARKET,
        action_type=str(action_type),
        ex_date=ex_date,
        announced_at=announced or datetime(2026, 8, 1, tzinfo=UTC),
        source=SOURCE,
        **fields,
    )


def _twse_reference(
    close: Decimal,
    *,
    cash: Decimal = Decimal(0),
    stock: Decimal = Decimal(0),
    sub_ratio: Decimal = Decimal(0),
    sub_price: Decimal = Decimal(0),
) -> Decimal:
    """The exchange's published formula, written out independently.

    Deliberately not a call into the service: a test that reuses the code under
    test only proves the code is self-consistent.
    """
    return (close - cash + sub_ratio * sub_price) / (Decimal(1) + stock + sub_ratio)


# ============================================================== the formula
class TestDayFactor:
    def test_a_cash_dividend_removes_exactly_the_dividend(self, session):
        day = AdjustmentService(session).compute_day_factor(
            [_action(CorporateActionType.CASH_DIVIDEND, cash_dividend=Decimal("5"))],
            prior_close=Decimal("100"),
        )
        assert day.resolved
        assert day.factor == Decimal("0.95")

    def test_a_stock_dividend_dilutes_by_the_ratio(self, session):
        day = AdjustmentService(session).compute_day_factor(
            [_action(CorporateActionType.STOCK_DIVIDEND, stock_dividend=Decimal("0.1"))],
            prior_close=Decimal("100"),
        )
        assert day.resolved
        assert day.factor == Decimal(1) / Decimal("1.1")

    def test_a_split_is_a_stock_dividend_of_n_minus_one(self, session):
        svc = AdjustmentService(session)
        split = svc.compute_day_factor(
            [_action(CorporateActionType.SPLIT, split_ratio=Decimal("2"))],
            prior_close=Decimal("100"),
        )
        stock = svc.compute_day_factor(
            [_action(CorporateActionType.STOCK_DIVIDEND, stock_dividend=Decimal("1"))],
            prior_close=Decimal("100"),
        )
        assert split.factor == stock.factor == Decimal("0.5")

    def test_a_rights_issue_prices_in_the_subscription_money(self, session):
        close, ratio, price = Decimal("100"), Decimal("0.2"), Decimal("40")
        day = AdjustmentService(session).compute_day_factor(
            [
                _action(
                    CorporateActionType.RIGHTS_ISSUE,
                    subscription_ratio=ratio,
                    subscription_price=price,
                )
            ],
            prior_close=close,
        )
        expected = _twse_reference(close, sub_ratio=ratio, sub_price=price) / close
        assert day.resolved
        assert day.factor == expected

    def test_components_on_one_day_reproduce_the_published_formula(self, session):
        """The case the decomposition exists for: all three at once.

        Multiplying independently-derived component factors is only correct if
        they were derived to multiply. TWSE's formula puts the stock ratio and
        the subscription ratio in one denominator, so a naive product of
        textbook factors is off by the cross term.
        """
        close = Decimal("100")
        cash, stock = Decimal("2"), Decimal("0.5")
        ratio, price = Decimal("0.3"), Decimal("20")

        day = AdjustmentService(session).compute_day_factor(
            [
                _action(CorporateActionType.CASH_DIVIDEND, cash_dividend=cash, id_=1),
                _action(CorporateActionType.STOCK_DIVIDEND, stock_dividend=stock, id_=2),
                _action(
                    CorporateActionType.RIGHTS_ISSUE,
                    subscription_ratio=ratio,
                    subscription_price=price,
                    id_=3,
                ),
            ],
            prior_close=close,
        )
        expected = (
            _twse_reference(close, cash=cash, stock=stock, sub_ratio=ratio, sub_price=price) / close
        )
        assert day.resolved
        assert day.factor == expected

        naive = (
            (close - cash)
            / close
            * (Decimal(1) / (Decimal(1) + stock))
            * ((close + ratio * price) / (close * (Decimal(1) + ratio)))
        )
        assert naive != expected, "the naive product should differ; that is why this exists"

    def test_each_row_carries_its_share_and_the_shares_multiply_out(self, session):
        """Per-row factors exist so an adjusted price can be explained.

        They are only worth storing if their product is the day factor exactly —
        otherwise the explanation and the number disagree.
        """
        day = AdjustmentService(session).compute_day_factor(
            [
                _action(CorporateActionType.CASH_DIVIDEND, cash_dividend=Decimal("2"), id_=1),
                _action(CorporateActionType.STOCK_DIVIDEND, stock_dividend=Decimal("0.5"), id_=2),
                _action(
                    CorporateActionType.RIGHTS_ISSUE,
                    subscription_ratio=Decimal("0.3"),
                    subscription_price=Decimal("20"),
                    id_=3,
                ),
            ],
            prior_close=Decimal("100"),
        )
        product = Decimal(1)
        for share in day.components.values():
            product *= share
        assert set(day.components) == {1, 2, 3}
        assert product == day.factor

    def test_a_capital_reduction_uses_the_published_price_effect(self, session):
        """The one event whose factor is taken from the exchange directly.

        Its ratio and any cash returned are two unknowns behind one observable,
        so the alternative to using the published prices is inventing one of
        them.
        """
        day = AdjustmentService(session).compute_day_factor(
            [
                _action(
                    CorporateActionType.CAPITAL_REDUCTION,
                    reference_price_before=Decimal("11.70"),
                    reference_price_after=Decimal("15.94"),
                )
            ]
        )
        assert day.resolved
        assert day.factor == Decimal("15.94") / Decimal("11.70")
        assert day.factor > 1, "cancelling shares raises the price"

    def test_the_exchanges_own_close_is_preferred_over_ours(self, session):
        day = AdjustmentService(session).compute_day_factor(
            [
                _action(
                    CorporateActionType.CASH_DIVIDEND,
                    cash_dividend=Decimal("5"),
                    reference_price_before=Decimal("100"),
                    reference_price_after=Decimal("95"),
                )
            ],
            prior_close=Decimal("123.45"),
        )
        assert day.base_price == Decimal("100")
        assert day.base_price_source == "EXCHANGE_REFERENCE"


# ============================================================ what it refuses
class TestRefusal:
    def test_a_rights_issue_with_no_price_yet_is_unresolved(self, session):
        """`尚未公告` is a real event with one unpublished term.

        Treating the missing price as zero would be a 100% discount and an
        enormous fabricated adjustment; treating it as absent would drop a real
        event. It blocks instead.
        """
        day = AdjustmentService(session).compute_day_factor(
            [
                _action(
                    CorporateActionType.RIGHTS_ISSUE,
                    subscription_ratio=Decimal("0.00996627"),
                    subscription_price=None,
                )
            ],
            prior_close=Decimal("50"),
        )
        assert not day.resolved
        assert day.factor == 1
        assert "subscription price" in day.unresolved[0]

    def test_a_cash_dividend_with_no_price_anywhere_is_unresolved(self, session):
        day = AdjustmentService(session).compute_day_factor(
            [_action(CorporateActionType.CASH_DIVIDEND, cash_dividend=Decimal("5"))],
            prior_close=None,
        )
        assert not day.resolved
        assert "pre-event price" in day.unresolved[0]

    def test_a_reduction_with_no_published_prices_is_unresolved(self, session):
        day = AdjustmentService(session).compute_day_factor(
            [_action(CorporateActionType.CAPITAL_REDUCTION)], prior_close=Decimal("20")
        )
        assert not day.resolved

    def test_an_absurd_factor_is_refused_rather_than_applied(self, session):
        """A parse error that survives to here would rewrite a decade of prices."""
        day = AdjustmentService(session).compute_day_factor(
            [_action(CorporateActionType.STOCK_DIVIDEND, stock_dividend=Decimal("500"))],
            prior_close=Decimal("100"),
        )
        assert not day.resolved
        assert "outside" in day.unresolved[0]

    def test_a_par_value_change_moves_nothing(self, session):
        day = AdjustmentService(session).compute_day_factor(
            [_action(CorporateActionType.PAR_VALUE_CHANGE, split_ratio=Decimal("1"))],
            prior_close=Decimal("100"),
        )
        assert day.resolved
        assert day.factor == 1


# ====================================================== against real payloads
class TestReconciliationAgainstTWSE:
    def test_computed_factors_agree_with_the_exchanges_own_reference_prices(self, session):
        """The check that matters: real events, real published answers.

        Every 息 row in the recording carries 除權息前收盤價 and 除權息參考價 —
        the exchange's own before and after. The factor computed here from the
        dividend alone must land on the same number.
        """
        payload = json.loads((FIXTURES / "twt49u_2026.json").read_text(encoding="utf-8"))
        records, _ = parse_results(payload)
        assert records, "the recording no longer contains any 息 rows"

        svc = AdjustmentService(session)
        checked = 0
        for n, rec in enumerate(records):
            if rec.get("reference_price_after") is None:
                continue
            day = svc.compute_day_factor(
                [
                    _action(
                        CorporateActionType.CASH_DIVIDEND,
                        ex_date=rec["ex_date"],
                        symbol=rec["symbol"],
                        id_=n + 1,
                        cash_dividend=rec["cash_dividend"],
                        reference_price_before=rec["reference_price_before"],
                        reference_price_after=rec["reference_price_after"],
                    )
                ]
            )
            assert day.resolved, day.unresolved
            assert day.residual_bps is not None
            assert day.reconciled, (
                f"{rec['symbol']} {rec['ex_date']}: computed {day.factor}, "
                f"exchange published {day.published} "
                f"({day.residual_bps} bps > {RECONCILIATION_TOLERANCE_BPS})"
            )
            checked += 1
        assert checked, "no row in the recording carried both reference prices"

    def test_a_wrong_dividend_is_caught_by_the_reconciliation(self, session):
        """The check has to be able to fail, or it is not a check."""
        day = AdjustmentService(session).compute_day_factor(
            [
                _action(
                    CorporateActionType.CASH_DIVIDEND,
                    cash_dividend=Decimal("10"),  # the exchange's prices say 5
                    reference_price_before=Decimal("100"),
                    reference_price_after=Decimal("95"),
                )
            ]
        )
        assert day.resolved
        assert day.reconciled is False
        assert day.residual_bps is not None and day.residual_bps > 500


# ================================================================== the series
async def _prices(session, symbol: str, days: list[date], close: Decimal) -> None:
    for day in days:
        session.add(
            DailyPrice(
                symbol=symbol,
                market=MARKET,
                trading_date=day,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1000,
                source="TWSE",
            )
        )
    await session.flush()


async def _cover(session, symbol: str = SYMBOL) -> None:
    await CorporateActionCoverageService(session).record(
        symbol=symbol,
        market=MARKET,
        source=SOURCE,
        covered_from=date(2026, 1, 1),
        covered_to=date(2026, 12, 31),
        action_types=PRICE_AFFECTING,
        actions_found=1,
    )
    await session.flush()


WEEK = [date(2026, 8, d) for d in (10, 11, 12, 13, 14)]


class TestAppliedSeries:
    async def test_adjustment_is_refused_without_coverage(self, session):
        await _prices(session, SYMBOL, WEEK, Decimal("100"))
        with pytest.raises(AdjustmentNotPermitted):
            await AdjustmentService(session).apply([SYMBOL], start=WEEK[0], end=WEEK[-1])

    async def test_bars_before_the_ex_date_are_restated_and_later_ones_are_not(self, session):
        await _prices(session, SYMBOL, WEEK, Decimal("100"))
        await _cover(session)
        session.add(
            _action(
                CorporateActionType.CASH_DIVIDEND,
                id_=None,
                ex_date=date(2026, 8, 13),
                cash_dividend=Decimal("5"),
                reference_price_before=Decimal("100"),
                reference_price_after=Decimal("95"),
            )
        )
        await session.flush()

        report = await AdjustmentService(session).apply([SYMBOL], start=WEEK[0], end=WEEK[-1])
        assert report.blocked == []
        assert report.rows_written == 5

        rows = await _series(session, SYMBOL)
        # Before the ex-date: restated into post-dividend terms.
        for day in WEEK[:3]:
            assert rows[day].adjust_factor == Decimal("0.9500000000")
            assert rows[day].adjusted_close == Decimal("95.0000")
            assert rows[day].close == Decimal("100.0000"), "the raw close is never overwritten"
        # On and after it: already in those terms, so the factor is exactly 1
        # rather than null. Null would mean nobody computed it.
        for day in WEEK[3:]:
            assert rows[day].adjust_factor == Decimal("1.0000000000")
            assert rows[day].adjusted_close == Decimal("100.0000")

    async def test_two_events_compound(self, session):
        await _prices(session, SYMBOL, WEEK, Decimal("100"))
        await _cover(session)
        session.add(
            _action(
                CorporateActionType.CASH_DIVIDEND,
                id_=None,
                ex_date=date(2026, 8, 12),
                cash_dividend=Decimal("5"),
                reference_price_before=Decimal("100"),
                reference_price_after=Decimal("95"),
            )
        )
        session.add(
            _action(
                CorporateActionType.CASH_DIVIDEND,
                id_=None,
                ex_date=date(2026, 8, 14),
                cash_dividend=Decimal("10"),
                reference_price_before=Decimal("100"),
                reference_price_after=Decimal("90"),
            )
        )
        await session.flush()

        await AdjustmentService(session).apply([SYMBOL], start=WEEK[0], end=WEEK[-1])
        rows = await _series(session, SYMBOL)
        assert rows[WEEK[0]].adjust_factor == Decimal("0.8550000000")  # 0.95 × 0.90
        assert rows[WEEK[2]].adjust_factor == Decimal("0.9000000000")
        assert rows[WEEK[4]].adjust_factor == Decimal("1.0000000000")

    async def test_running_it_twice_changes_nothing(self, session):
        await _prices(session, SYMBOL, WEEK, Decimal("100"))
        await _cover(session)
        session.add(
            _action(
                CorporateActionType.CASH_DIVIDEND,
                id_=None,
                ex_date=date(2026, 8, 13),
                cash_dividend=Decimal("5"),
                reference_price_before=Decimal("100"),
                reference_price_after=Decimal("95"),
            )
        )
        await session.flush()

        svc = AdjustmentService(session)
        await svc.apply([SYMBOL], start=WEEK[0], end=WEEK[-1])
        first = {d: r.adjusted_close for d, r in (await _series(session, SYMBOL)).items()}
        await svc.apply([SYMBOL], start=WEEK[0], end=WEEK[-1])
        second = {d: r.adjusted_close for d, r in (await _series(session, SYMBOL)).items()}
        assert first == second

    async def test_an_unresolvable_event_leaves_the_whole_symbol_alone(self, session):
        """Partial adjustment is the dangerous outcome, so there is none.

        A series adjusted for the dividend but not for the rights issue three
        days later looks exactly like a correct one and is wrong by the size of
        the event nobody could price.
        """
        await _prices(session, SYMBOL, WEEK, Decimal("100"))
        await _cover(session)
        session.add(
            _action(
                CorporateActionType.CASH_DIVIDEND,
                id_=None,
                ex_date=date(2026, 8, 12),
                cash_dividend=Decimal("5"),
            )
        )
        session.add(
            _action(
                CorporateActionType.RIGHTS_ISSUE,
                id_=None,
                ex_date=date(2026, 8, 14),
                subscription_ratio=Decimal("0.1"),
                subscription_price=None,
            )
        )
        await session.flush()

        report = await AdjustmentService(session).apply([SYMBOL], start=WEEK[0], end=WEEK[-1])
        assert report.blocked == [SYMBOL]
        assert report.rows_written == 0
        rows = await _series(session, SYMBOL)
        assert all(r.adjust_factor is None for r in rows.values())
        assert all(r.adjusted_close is None for r in rows.values())

    async def test_factors_are_written_back_onto_the_actions(self, session):
        await _prices(session, SYMBOL, WEEK, Decimal("100"))
        await _cover(session)
        action = _action(
            CorporateActionType.CASH_DIVIDEND,
            id_=None,
            ex_date=date(2026, 8, 13),
            cash_dividend=Decimal("5"),
            reference_price_before=Decimal("100"),
            reference_price_after=Decimal("95"),
        )
        session.add(action)
        await session.flush()
        assert action.factor == 1, "the column defaults to a no-op until computed"

        await AdjustmentService(session).apply([SYMBOL], start=WEEK[0], end=WEEK[-1])
        await session.refresh(action)
        assert action.factor == Decimal("0.9500000000")

    async def test_an_action_the_market_did_not_know_yet_is_excluded(self, session):
        """Point in time. The announcement is the clock, not the ex-date."""
        await _prices(session, SYMBOL, WEEK, Decimal("100"))
        session.add(
            _action(
                CorporateActionType.CASH_DIVIDEND,
                id_=None,
                ex_date=date(2026, 8, 13),
                announced=datetime(2026, 8, 5, tzinfo=UTC),
                cash_dividend=Decimal("5"),
                reference_price_before=Decimal("100"),
                reference_price_after=Decimal("95"),
            )
        )
        await session.flush()

        svc = AdjustmentService(session)
        before = await svc.compute_factors(
            [SYMBOL], after=WEEK[0], as_of=datetime(2026, 8, 4, tzinfo=UTC), persist=False
        )
        after = await svc.compute_factors(
            [SYMBOL], after=WEEK[0], as_of=datetime(2026, 8, 6, tzinfo=UTC), persist=False
        )
        assert before == []
        assert len(after) == 1


async def _series(session, symbol: str) -> dict[date, DailyPrice]:
    from sqlalchemy import select

    rows = (await session.execute(select(DailyPrice).where(DailyPrice.symbol == symbol))).scalars()
    return {r.trading_date: r for r in rows}


# ====================================== an event announced but not yet priced
class TestUnpricedEvents:
    """TWSE lists a capital reduction before it happens, with every price
    column as `-`. Where that record goes decides whether the adjustment can
    ever see it."""

    def test_a_future_unpriced_event_is_stored_with_a_warning(self):
        from app.services.quality_service import DataQualityService

        result = DataQualityService().validate_corporate_action(
            {
                "symbol": "1563",
                "market": MARKET,
                "action_type": CorporateActionType.CAPITAL_REDUCTION,
                "ex_date": date(2026, 9, 7),
                "announced_at": datetime(2026, 8, 19, tzinfo=UTC),
                "reference_price_before": None,
                "reference_price_after": None,
            },
            today=date(2026, 8, 19),
        )
        assert result.accepted, (
            "quarantining it would put the event outside corporate_actions, the "
            "coverage guard would pass, and the series would be adjusted over "
            "that date without it"
        )
        assert result.quality_status == "SUSPECT"
        assert "CA09" in result.flags

    def test_a_past_event_with_no_terms_is_a_parser_failure(self):
        from app.services.quality_service import DataQualityService

        result = DataQualityService().validate_corporate_action(
            {
                "symbol": "1563",
                "market": MARKET,
                "action_type": CorporateActionType.CAPITAL_REDUCTION,
                "ex_date": date(2026, 1, 7),
                "announced_at": datetime(2026, 1, 1, tzinfo=UTC),
            },
            today=date(2026, 8, 19),
        )
        assert not result.accepted, "the terms should exist for a past event"
        assert "CA06" in result.flags

    async def test_the_stored_event_then_blocks_its_symbol(self, session):
        """Storing it is only correct if something downstream refuses it."""
        await _prices(session, SYMBOL, WEEK, Decimal("100"))
        await _cover(session)
        session.add(
            _action(
                CorporateActionType.CAPITAL_REDUCTION,
                id_=None,
                ex_date=date(2026, 8, 13),
            )
        )
        await session.flush()

        report = await AdjustmentService(session).apply([SYMBOL], start=WEEK[0], end=WEEK[-1])
        assert report.blocked == [SYMBOL]
        assert "reference prices" in report.unresolved[0].unresolved[0]
