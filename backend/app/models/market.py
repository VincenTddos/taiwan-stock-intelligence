"""Canonical market data model.

Three properties are enforced structurally rather than by convention:

1. **Raw prices are immutable.** `daily_prices` stores exactly what the exchange
   published. Adjustment for corporate actions is a *derived* column computed
   from `corporate_actions`, never an overwrite. A backtest that needs the price
   someone actually paid on the day and a backtest that needs a continuous
   return series are both served, and neither corrupts the other.

2. **Every row knows where it came from.** `source` plus a foreign key to the
   `raw_ingestions` record that produced it. Any number in this schema can be
   traced back to an HTTP response.

3. **The stock master is slowly-changing.** Names, industries and listing status
   change. Storing only the current value destroys the ability to ask what a
   company was called, or which industry it was in, on a past date — which is
   exactly what a historical study needs.
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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, provenance_column

# Money keeps four decimal places: TWSE quotes to two, but adjusted prices
# accumulate factors and would lose cents to rounding at two.
MONEY = Numeric(18, 4)
RATIO = Numeric(18, 10)


class SessionType(StrEnum):
    FULL = "FULL"
    HALF = "HALF"
    CLOSED = "CLOSED"
    SETTLEMENT_ONLY = "SETTLEMENT_ONLY"  # 市場無交易，僅辦理結算交割


class ListingStatus(StrEnum):
    LISTED = "LISTED"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"


class QualityStatus(StrEnum):
    """Set by DataQualityService. Suspect rows are kept and flagged, never deleted."""

    OK = "OK"
    SUSPECT = "SUSPECT"
    INVALID = "INVALID"


class CorporateActionType(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    CAPITAL_REDUCTION = "CAPITAL_REDUCTION"
    PAR_VALUE_CHANGE = "PAR_VALUE_CHANGE"


# --------------------------------------------------------------------------
class TradingCalendar(Base):
    """One row per calendar day per market.

    `is_trading_day` is authoritative for every ingestion job. A job never infers
    a trading date from its own execution date — see `TradingCalendarService`.
    """

    __tablename__ = "trading_calendar"

    market: Mapped[str] = mapped_column(String(10), primary_key=True)
    calendar_date: Mapped[date] = mapped_column(Date, primary_key=True)

    is_trading_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    session_type: Mapped[str] = mapped_column(String(20), nullable=False, default=SessionType.FULL)
    holiday_name: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)

    # True once corroborated by observed market activity on that date. A calendar
    # derived purely from a published schedule is a prediction; volume is evidence.
    verified_by_volume: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    observed_symbol_count: Mapped[int | None] = mapped_column(Integer)

    source: Mapped[str] = mapped_column(String(20), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "session_type IN ('FULL','HALF','CLOSED','SETTLEMENT_ONLY')",
            name="session_type_allowed",
        ),
        CheckConstraint(
            "(is_trading_day AND session_type IN ('FULL','HALF')) "
            "OR (NOT is_trading_day AND session_type IN ('CLOSED','SETTLEMENT_ONLY'))",
            name="trading_day_matches_session",
        ),
        Index("ix_trading_calendar_trading", "market", "calendar_date", "is_trading_day"),
    )


# --------------------------------------------------------------------------
class StockMaster(Base):
    """Slowly-changing dimension (type 2).

    A symbol has many rows over time; at most one is current (`valid_to IS NULL`).
    Querying "what was 2330 called on 2019-06-01" is
    `valid_from <= :d AND (valid_to IS NULL OR valid_to > :d)`.
    """

    __tablename__ = "stock_master"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(50))
    name_en: Mapped[str | None] = mapped_column(String(150))
    short_name_en: Mapped[str | None] = mapped_column(String(50))

    security_type: Mapped[str] = mapped_column(String(20), nullable=False, default="COMMON")
    industry_code: Mapped[str | None] = mapped_column(String(10))
    industry_name: Mapped[str | None] = mapped_column(String(50))
    sector: Mapped[str | None] = mapped_column(String(50))

    listing_date: Mapped[date | None] = mapped_column(Date)
    delisting_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ListingStatus.LISTED)

    par_value: Mapped[Decimal | None] = mapped_column(MONEY)
    paid_in_capital: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    shares_outstanding: Mapped[int | None] = mapped_column(BigInteger)
    tax_id: Mapped[str | None] = mapped_column(String(20))

    # --- SCD2 validity window (knowledge time, not event time) ---
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    attributes: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    source: Mapped[str] = mapped_column(String(20), nullable=False)
    ingestion_id: Mapped[int | None] = provenance_column()
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(r"symbol ~ '^[0-9A-Z]{4,10}$'", name="symbol_format"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="validity_window"),
        CheckConstraint("status IN ('LISTED','SUSPENDED','DELISTED')", name="status_allowed"),
        # At most one current row per symbol+market. A partial unique index,
        # because a plain constraint over (symbol, market, valid_to) would not
        # collide: NULL never equals NULL in SQL.
        Index(
            "uq_stock_master_current",
            "symbol",
            "market",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_stock_master_symbol", "symbol", "valid_from"),
        Index("ix_stock_master_industry", "industry_code"),
    )


# --------------------------------------------------------------------------
class DailyPrice(Base):
    """End-of-day OHLCV, exactly as published.

    `(symbol, trading_date, source)` is unique. Keeping `source` in the key is
    deliberate: when two exchanges or two endpoints disagree we want both rows so
    the conflict is visible and diagnosable, not one row silently overwriting the
    other. The API resolves a preferred source per market.
    """

    __tablename__ = "daily_prices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)

    # --- raw, immutable, as published -------------------------------------
    open: Mapped[Decimal | None] = mapped_column(MONEY)
    high: Mapped[Decimal | None] = mapped_column(MONEY)
    low: Mapped[Decimal | None] = mapped_column(MONEY)
    close: Mapped[Decimal | None] = mapped_column(MONEY)
    change: Mapped[Decimal | None] = mapped_column(MONEY)

    volume: Mapped[int | None] = mapped_column(BigInteger)
    turnover: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    trade_count: Mapped[int | None] = mapped_column(BigInteger)

    # --- derived from corporate_actions; recomputable, never authoritative --
    adjust_factor: Mapped[Decimal | None] = mapped_column(RATIO)
    adjusted_close: Mapped[Decimal | None] = mapped_column(MONEY)
    adjusted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(String(20))

    quality_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=QualityStatus.OK
    )
    quality_flags: Mapped[list[str] | None] = mapped_column(JSONB)

    source: Mapped[str] = mapped_column(String(20), nullable=False)
    ingestion_id: Mapped[int | None] = provenance_column()
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("symbol", "trading_date", "source", name="uq_daily_prices_key"),
        CheckConstraint(r"symbol ~ '^[0-9A-Z]{4,10}$'", name="symbol_format"),
        CheckConstraint("volume IS NULL OR volume >= 0", name="volume_non_negative"),
        CheckConstraint("turnover IS NULL OR turnover >= 0", name="turnover_non_negative"),
        CheckConstraint(
            "quality_status IN ('OK','SUSPECT','INVALID')", name="quality_status_allowed"
        ),
        # OHLC coherence is a *database* invariant. A row that violates it is not
        # stored at all — it goes to quarantine, where it can be inspected.
        CheckConstraint("high IS NULL OR low IS NULL OR high >= low", name="high_ge_low"),
        CheckConstraint("high IS NULL OR open IS NULL OR high >= open", name="high_ge_open"),
        CheckConstraint("high IS NULL OR close IS NULL OR high >= close", name="high_ge_close"),
        CheckConstraint("low IS NULL OR open IS NULL OR low <= open", name="low_le_open"),
        CheckConstraint("low IS NULL OR close IS NULL OR low <= close", name="low_le_close"),
        Index("ix_daily_prices_symbol_date", "symbol", "trading_date"),
        Index("ix_daily_prices_date", "trading_date"),
        Index("ix_daily_prices_quality", "quality_status"),
    )


# --------------------------------------------------------------------------
class IndexQuote(Base):
    """Index close. Structured so that industry and sector indices need no schema change."""

    __tablename__ = "index_quotes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    index_code: Mapped[str] = mapped_column(String(40), nullable=False)
    index_name: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    index_type: Mapped[str] = mapped_column(String(20), nullable=False, default="MARKET")
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)

    open: Mapped[Decimal | None] = mapped_column(MONEY)
    high: Mapped[Decimal | None] = mapped_column(MONEY)
    low: Mapped[Decimal | None] = mapped_column(MONEY)
    close: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    change: Mapped[Decimal | None] = mapped_column(MONEY)
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))

    volume: Mapped[int | None] = mapped_column(BigInteger)
    turnover: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))

    quality_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=QualityStatus.OK
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    ingestion_id: Mapped[int | None] = provenance_column()
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("index_code", "trading_date", "source", name="uq_index_quotes_key"),
        CheckConstraint(
            "index_type IN ('MARKET','INDUSTRY','SECTOR','THEMATIC')", name="index_type_allowed"
        ),
        Index("ix_index_quotes_code_date", "index_code", "trading_date"),
        Index("ix_index_quotes_date", "trading_date"),
    )


# --------------------------------------------------------------------------
class InstitutionalFlow(Base):
    """Daily institutional buy/sell by investor category.

    Buy and sell volumes are stored separately, not just the net. Net alone
    discards turnover: 1M bought against 1M sold and zero activity both net to
    zero, and they mean completely different things.
    """

    __tablename__ = "institutional_flow"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    investor_type: Mapped[str] = mapped_column(String(30), nullable=False)

    buy_volume: Mapped[int | None] = mapped_column(BigInteger)
    sell_volume: Mapped[int | None] = mapped_column(BigInteger)
    net_volume: Mapped[int | None] = mapped_column(BigInteger)

    # Populated once the day's close is known; not published by the source.
    buy_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    sell_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    net_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))

    quality_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=QualityStatus.OK
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    ingestion_id: Mapped[int | None] = provenance_column()
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol", "trading_date", "investor_type", "source", name="uq_institutional_flow_key"
        ),
        CheckConstraint(
            "investor_type IN ('FOREIGN','FOREIGN_DEALER','INVESTMENT_TRUST',"
            "'DEALER_SELF','DEALER_HEDGE','DEALER','TOTAL')",
            name="investor_type_allowed",
        ),
        CheckConstraint(r"symbol ~ '^[0-9A-Z]{4,10}$'", name="symbol_format"),
        Index("ix_institutional_flow_symbol_date", "symbol", "trading_date"),
        Index("ix_institutional_flow_date_type", "trading_date", "investor_type"),
    )


# --------------------------------------------------------------------------
class CorporateAction(Base):
    """Bitemporal, like every other fact that a backtest can see.

    `ex_date` is when the adjustment takes effect; `announced_at` is when the
    market could know about it. Adjustment factors applied to a historical series
    must only use actions announced by the simulated date, or the backtest is
    reading the future.
    """

    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)

    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    announced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    announced_at_is_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payment_date: Mapped[date | None] = mapped_column(Date)
    record_date: Mapped[date | None] = mapped_column(Date)

    # Per-share economics. Which of these are populated is fixed by
    # `action_type`; see EXPECTED_FIELDS in providers/corporate_actions.py.
    cash_dividend: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    stock_dividend: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    split_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    subscription_price: Mapped[Decimal | None] = mapped_column(MONEY)
    subscription_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    # A cash capital reduction returns money *and* cancels shares, so it needs
    # both this and `split_ratio`. It is kept apart from `cash_dividend` because
    # the two are not the same event: a dividend is a distribution of earnings
    # and a reduction is a return of capital. They are taxed differently, they
    # are analysed differently, and collapsing them would make it impossible to
    # tell afterwards which one happened.
    cash_returned_per_share: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    # What the exchange itself published either side of the event, where the
    # source provides it. These are never used to compute the adjustment — they
    # are used to check it. A factor derived from the economics and then
    # reconciled against the exchange's own reference price is a factor that can
    # be shown to be right rather than merely asserted.
    reference_price_before: Mapped[Decimal | None] = mapped_column(MONEY)
    reference_price_after: Mapped[Decimal | None] = mapped_column(MONEY)

    # Single-day factor. The cumulative factor on a price row is the running
    # product of these, computed backwards from the present.
    factor: Mapped[Decimal] = mapped_column(RATIO, nullable=False, default=Decimal(1))

    source: Mapped[str] = mapped_column(String(20), nullable=False)
    ingestion_id: Mapped[int | None] = provenance_column()
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol", "action_type", "ex_date", "source", name="uq_corporate_actions_key"
        ),
        CheckConstraint(
            "action_type IN ('CASH_DIVIDEND','STOCK_DIVIDEND','SPLIT','REVERSE_SPLIT',"
            "'RIGHTS_ISSUE','CAPITAL_REDUCTION','PAR_VALUE_CHANGE')",
            name="action_type_allowed",
        ),
        CheckConstraint("factor > 0", name="factor_positive"),
        CheckConstraint(
            "(reference_price_before IS NULL OR reference_price_before > 0) AND "
            "(reference_price_after IS NULL OR reference_price_after > 0)",
            name="reference_prices_positive",
        ),
        CheckConstraint(
            "cash_returned_per_share IS NULL OR cash_returned_per_share >= 0",
            name="cash_returned_non_negative",
        ),
        Index("ix_corporate_actions_symbol", "symbol", "ex_date"),
        Index("ix_corporate_actions_pit", "symbol", "announced_at"),
    )


# --------------------------------------------------------------------------
class CorporateActionCoverage(Base):
    """What we have actually looked for, per symbol, and over what range.

    Without this table, an empty `corporate_actions` result is ambiguous in the
    worst possible way: "this company paid no dividend" and "nobody has ever
    fetched dividends for this company" are the same answer. Adjusting a price
    series on the first reading when the truth is the second silently deletes a
    real adjustment, and the resulting return series looks perfectly plausible.

    So coverage is recorded as a positive fact. The adjustment pipeline refuses
    to run outside it — see `CorporateActionCoverageService.assert_adjustable`.

    `action_types` is stored because sources differ in what they can see. A
    source that reports dividends but not capital reductions gives real coverage
    for one and none for the other, and a single boolean cannot say that.
    """

    __tablename__ = "corporate_action_coverage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)

    covered_from: Mapped[date] = mapped_column(Date, nullable=False)
    covered_to: Mapped[date] = mapped_column(Date, nullable=False)
    action_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)

    actions_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    ingestion_id: Mapped[int | None] = provenance_column()
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("symbol", "market", "source", name="uq_corporate_action_coverage_key"),
        CheckConstraint("covered_to >= covered_from", name="coverage_window"),
        CheckConstraint("actions_found >= 0", name="actions_found_non_negative"),
        Index("ix_corporate_action_coverage_symbol", "symbol", "market"),
    )


# --------------------------------------------------------------------------
class MarketStatus(Base):
    """Per-market operational snapshot, refreshed by ingestion jobs.

    This is what `/api/v1/market/status` reads. It never calls a provider.
    """

    __tablename__ = "market_status"

    market: Mapped[str] = mapped_column(String(10), primary_key=True)

    last_trading_date: Mapped[date | None] = mapped_column(Date)
    next_trading_date: Mapped[date | None] = mapped_column(Date)
    is_trading_day_today: Mapped[bool | None] = mapped_column(Boolean)
    session_type_today: Mapped[str | None] = mapped_column(String(20))

    symbol_count: Mapped[int | None] = mapped_column(Integer)
    price_row_count: Mapped[int | None] = mapped_column(BigInteger)
    earliest_price_date: Mapped[date | None] = mapped_column(Date)
    latest_price_date: Mapped[date | None] = mapped_column(Date)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = [
    "MONEY",
    "RATIO",
    "CorporateAction",
    "CorporateActionType",
    "DailyPrice",
    "IndexQuote",
    "InstitutionalFlow",
    "ListingStatus",
    "MarketStatus",
    "QualityStatus",
    "SessionType",
    "StockMaster",
    "TradingCalendar",
]
