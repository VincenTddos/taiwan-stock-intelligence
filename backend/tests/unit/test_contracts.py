"""The data contracts encode two invariants that the rest of the platform
depends on. If these tests fail, Phase 2 onwards is unsafe."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.contracts import (
    AIStockScore,
    DataSource,
    FinancialFact,
    HistoricalPrice,
    Market,
    MarketQuote,
    Provenance,
    ScoreContribution,
    StatementType,
    Timeframe,
)


def _fact(**kw: object) -> FinancialFact:
    base: dict[str, object] = {
        "symbol": "2330",
        "statement_type": StatementType.INCOME,
        "period_end": date(2026, 6, 30),
        "announced_at": datetime(2026, 8, 14, 9, 32, tzinfo=UTC),
        "metric": "revenue",
        "value": Decimal("1000"),
        "source": DataSource.TWSE,
    }
    base.update(kw)
    return FinancialFact(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------- bitemporality
def test_financial_fact_requires_announced_at():
    """A fundamental record without a disclosure time cannot be used safely,
    so the type refuses to represent one."""
    with pytest.raises(ValidationError):
        FinancialFact(  # type: ignore[call-arg]
            symbol="2330",
            statement_type=StatementType.INCOME,
            period_end=date(2026, 6, 30),
            metric="revenue",
            source=DataSource.TWSE,
        )


def test_announcement_cannot_precede_period_end():
    with pytest.raises(ValidationError, match="precedes"):
        _fact(announced_at=datetime(2026, 5, 1, tzinfo=UTC))


def test_is_known_at_uses_announcement_not_period():
    fact = _fact()
    # The quarter ended 2026-06-30 but was only disclosed on 2026-08-14.
    assert fact.is_known_at(date(2026, 6, 30)) is False
    assert fact.is_known_at(date(2026, 8, 13)) is False
    assert fact.is_known_at(date(2026, 8, 14)) is True
    assert fact.is_known_at(date(2026, 9, 1)) is True


def test_backtest_filter_excludes_future_information():
    """The exact pattern every historical simulation must use."""
    facts = [
        _fact(period_end=date(2026, 3, 31), announced_at=datetime(2026, 5, 14, tzinfo=UTC)),
        _fact(period_end=date(2026, 6, 30), announced_at=datetime(2026, 8, 14, tzinfo=UTC)),
    ]
    as_of = date(2026, 7, 1)
    visible = [f for f in facts if f.is_known_at(as_of)]

    assert len(visible) == 1
    assert visible[0].period_end == date(2026, 3, 31)
    # Naively filtering on period_end would have leaked the Q2 report.
    naive = [f for f in facts if f.period_end <= as_of]
    assert len(naive) == 2


# --------------------------------------------------------------- provenance
def test_derived_values_require_provenance():
    with pytest.raises(ValidationError):
        AIStockScore(  # type: ignore[call-arg]
            symbol="2330", trading_date=date(2026, 8, 15), total_score=Decimal("91.2")
        )


def _score(contribs: list[ScoreContribution], total: str = "91.2") -> AIStockScore:
    return AIStockScore(
        symbol="2330",
        trading_date=date(2026, 8, 15),
        total_score=Decimal(total),
        contributions=contribs,
        provenance=Provenance(
            model_version="stockrank-v1.4",
            dataset_version="2026-08-15",
            feature_version="core-v1.2",
            calculated_at=datetime.now(UTC),
            data_as_of=date(2026, 8, 15),
            source=[DataSource.DERIVED],
        ),
    )


def _c(name: str, value: str) -> ScoreContribution:
    return ScoreContribution(
        component=name, label_zh=name, contribution=Decimal(value), weight=Decimal("0.1")
    )


def test_contribution_balance_invariant_holds():
    score = _score([_c("a", "30.0"), _c("b", "15.0"), _c("c", "-3.8")])
    assert score.contributions_balance() is True  # 50 + 30 + 15 - 3.8 == 91.2


def test_contribution_balance_invariant_detects_mismatch():
    score = _score([_c("a", "10.0")])
    assert score.contributions_balance() is False


def test_empty_contributions_are_not_a_false_pass():
    """No explanation is allowed (score without breakdown yet), but a wrong
    explanation is not."""
    assert _score([]).contributions_balance() is True


# ------------------------------------------------------------ demo honesty
def test_mock_source_must_be_flagged_as_demo():
    with pytest.raises(ValidationError, match="is_demo"):
        MarketQuote(
            symbol="2330",
            market=Market.TWSE,
            quoted_at=datetime.now(UTC),
            trading_date=date(2026, 8, 15),
            source=DataSource.MOCK,
            is_demo=False,
        )


def test_mock_source_with_demo_flag_is_allowed():
    q = MarketQuote(
        symbol="2330",
        market=Market.TWSE,
        quoted_at=datetime.now(UTC),
        trading_date=date(2026, 8, 15),
        source=DataSource.MOCK,
        is_demo=True,
    )
    assert q.is_demo is True


# ----------------------------------------------------------------- validity
@pytest.mark.parametrize("symbol", ["2330", "00981A", "3017"])
def test_valid_symbols(symbol: str):
    assert (
        MarketQuote(
            symbol=symbol,
            market=Market.TWSE,
            quoted_at=datetime.now(UTC),
            trading_date=date(2026, 8, 15),
            source=DataSource.TWSE,
        ).symbol
        == symbol
    )


@pytest.mark.parametrize("symbol", ["23", "abc", "2330 ", "台積電", ""])
def test_invalid_symbols_rejected(symbol: str):
    with pytest.raises(ValidationError):
        MarketQuote(
            symbol=symbol,
            market=Market.TWSE,
            quoted_at=datetime.now(UTC),
            trading_date=date(2026, 8, 15),
            source=DataSource.TWSE,
        )


def test_ohlc_consistency_enforced():
    with pytest.raises(ValidationError):
        HistoricalPrice(
            symbol="2330",
            market=Market.TWSE,
            trading_date=date(2026, 8, 15),
            timeframe=Timeframe.D1,
            adjusted=True,
            open=Decimal("100"),
            high=Decimal("99"),
            low=Decimal("98"),
            close=Decimal("98.5"),
            source=DataSource.TWSE,
        )


def test_prices_are_decimal_not_float():
    bar = HistoricalPrice(
        symbol="2330",
        market=Market.TWSE,
        trading_date=date(2026, 8, 15),
        adjusted=False,
        close=Decimal("2505.00"),
        source=DataSource.TWSE,
    )
    assert isinstance(bar.close, Decimal)
