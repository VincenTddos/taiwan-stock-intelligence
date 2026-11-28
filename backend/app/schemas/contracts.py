"""Shared data contracts.

Phase 1 has no market data. These models exist anyway, because the failure mode
we are preventing is structural: frontend, backend and quant each inventing
their own idea of what "a price" or "a financial fact" is. Every future module
imports from here; nobody redefines them locally.

Two rules are baked into the types rather than left to discipline:

1. **Bitemporality.** `FinancialFact` cannot be constructed without
   `announced_at`. A fact whose disclosure time is unknown cannot be used
   safely in a backtest, so the type refuses to represent it.

2. **Provenance.** Anything computed (`FactorScore`, `AIStockScore`) carries a
   `Provenance` block. There is no code path that produces a score without
   recording which model, dataset and feature version made it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

SYMBOL_PATTERN = r"^[0-9A-Z]{4,10}$"
Symbol = Annotated[str, Field(pattern=SYMBOL_PATTERN, description="Exchange ticker, e.g. 2330")]

# Money and prices are Decimal, never float. Binary floating point silently
# loses cents, and cents compound across a ten-year backtest.
Money = Annotated[Decimal, Field(max_digits=20, decimal_places=4)]
Ratio = Annotated[Decimal, Field(max_digits=12, decimal_places=6)]


class Market(StrEnum):
    TWSE = "TWSE"
    TPEX = "TPEX"
    EMERGING = "EMERGING"
    US = "US"


class DataSource(StrEnum):
    TWSE = "TWSE"
    TPEX = "TPEX"
    TAIFEX = "TAIFEX"
    MOPS = "MOPS"
    NEWS = "NEWS"
    DERIVED = "DERIVED"
    MOCK = "MOCK"  # dev/test only; surfaces as is_demo=true all the way to the UI


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    D1 = "1d"
    W1 = "1w"
    MO1 = "1mo"


class RelationType(StrEnum):
    DIRECT = "DIRECT"
    SUPPLIER = "SUPPLIER"
    CUSTOMER = "CUSTOMER"
    COMPETITOR = "COMPETITOR"
    INDUSTRY = "INDUSTRY"
    THEMATIC = "THEMATIC"
    MACRO = "MACRO"


class Provenance(BaseModel):
    """Reproducibility Rule — attached to every derived value.

    Given these five fields plus the git sha recorded in `model_versions`, any
    number in the system can be recomputed exactly.
    """

    model_config = ConfigDict(protected_namespaces=())

    model_version: str | None = None
    dataset_version: str | None = None
    feature_version: str | None = None
    calculated_at: datetime
    data_as_of: date

    source: list[DataSource] = Field(default_factory=list)
    is_demo: bool = False


class _Base(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), frozen=True)


# --------------------------------------------------------------------- market
class MarketQuote(_Base):
    """A point-in-time quote. Always delayed or end-of-day in this platform."""

    symbol: Symbol
    market: Market
    quoted_at: datetime = Field(description="Timezone-aware; stored UTC")
    trading_date: date

    price: Money | None = None
    open: Money | None = None
    high: Money | None = None
    low: Money | None = None
    prev_close: Money | None = None
    change: Money | None = None
    change_pct: Ratio | None = None
    volume: int | None = Field(default=None, ge=0)
    turnover: Money | None = None
    trade_count: int | None = Field(default=None, ge=0)

    is_suspended: bool = False
    is_limit_up: bool = False
    is_limit_down: bool = False

    source: DataSource
    is_demo: bool = False

    @model_validator(mode="after")
    def _mock_implies_demo(self) -> MarketQuote:
        if self.source is DataSource.MOCK and not self.is_demo:
            raise ValueError("MOCK-sourced data must set is_demo=True")
        return self


class HistoricalPrice(_Base):
    """One OHLCV bar. `adjusted` is explicit: silently mixing adjusted and raw
    series is one of the most common sources of wrong backtests."""

    symbol: Symbol
    market: Market
    trading_date: date
    timeframe: Timeframe = Timeframe.D1
    bar_start: datetime | None = None

    open: Money | None = None
    high: Money | None = None
    low: Money | None = None
    close: Money | None = None
    volume: int | None = Field(default=None, ge=0)
    turnover: Money | None = None
    trade_count: int | None = Field(default=None, ge=0)

    adjusted: bool = Field(description="True = corporate actions applied")
    adjust_factor: Decimal | None = None

    source: DataSource
    quality_flags: list[str] = Field(default_factory=list)
    is_demo: bool = False

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> HistoricalPrice:
        vals = [v for v in (self.open, self.high, self.low, self.close) if v is not None]
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("high < low")
        if vals and self.high is not None and max(vals) > self.high:
            raise ValueError("a price exceeds high")
        if vals and self.low is not None and min(vals) < self.low:
            raise ValueError("a price is below low")
        return self


# ----------------------------------------------------------------- fundamental
class StatementType(StrEnum):
    INCOME = "INCOME"
    BALANCE = "BALANCE"
    CASHFLOW = "CASHFLOW"
    MONTHLY_REVENUE = "MONTHLY_REVENUE"


class FinancialFact(_Base):
    """★ Bitemporal by construction.

    `period_end` is when the fact was *about*; `announced_at` is when the market
    could *know* it. Backtests filter on `announced_at` only — see
    `is_known_at()`. Both fields are required, so it is not possible to persist
    a fundamental record that cannot be used safely.
    """

    symbol: Symbol
    statement_type: StatementType
    period_end: date
    announced_at: datetime
    announced_at_is_estimated: bool = False

    fiscal_year: int | None = None
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    revision: int = 0
    is_restated: bool = False

    metric: str = Field(description="e.g. revenue, eps, gross_margin")
    value: Decimal | None = None
    unit: str = "TWD"
    currency: str = "TWD"

    source: DataSource
    is_demo: bool = False

    @model_validator(mode="after")
    def _announcement_after_period(self) -> FinancialFact:
        if self.announced_at.date() < self.period_end:
            raise ValueError(
                f"announced_at ({self.announced_at.date()}) precedes "
                f"period_end ({self.period_end}); this would leak future information"
            )
        return self

    def is_known_at(self, as_of: date) -> bool:
        """The only sanctioned availability test for historical simulation."""
        return self.announced_at.date() <= as_of


# ------------------------------------------------------------------- news
class NewsDocument(_Base):
    external_id: str | None = None
    url: str
    url_hash: str
    title: str
    summary: str | None = None
    lang: str = "zh"

    published_at: datetime = Field(description="Knowledge time for news")
    ingested_at: datetime

    source_code: str
    source_credibility: Ratio | None = None

    sentiment: Ratio | None = Field(default=None, description="[-1, 1]")
    sentiment_confidence: Ratio | None = None
    importance: Ratio | None = None

    cluster_id: int | None = None
    is_duplicate: bool = False

    related_symbols: list[Symbol] = Field(default_factory=list)
    provenance: Provenance | None = None


class ExtractionMethod(StrEnum):
    """How an entity was found in a story.

    The distinction that matters is between `SOURCE_ATTRIBUTED` and everything
    else. When an exchange announcement carries the company's own code, the link
    is the publisher's statement; a dictionary hit and a language model are both
    inferences. Collapsing the two would let an inference inherit an
    announcement's certainty.
    """

    DICTIONARY = "DICTIONARY"
    SOURCE_ATTRIBUTED = "SOURCE_ATTRIBUTED"
    LLM = "LLM"
    HYBRID = "HYBRID"


class TimeHorizon(StrEnum):
    INTRADAY = "INTRADAY"
    SHORT = "SHORT"
    MEDIUM = "MEDIUM"
    LONG = "LONG"


class EvidenceSnippet(_Base):
    """Where in the text a claim came from.

    Offsets, not just the quoted string, so the claim can be highlighted in the
    original rather than searched for again — a search would find the second
    occurrence as readily as the first.
    """

    text: str = Field(min_length=1, max_length=500)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def offsets_ordered(self) -> EvidenceSnippet:
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end <= self.char_start
        ):
            raise ValueError("char_end must be greater than char_start")
        return self


class NewsEntityMention(_Base):
    """One entity found in one story."""

    entity_type: str
    entity_text: str = Field(min_length=1, max_length=150)
    normalized: str | None = None

    symbol: Symbol | None = None
    extraction_method: ExtractionMethod
    confidence: Ratio = Field(ge=0, le=1)

    evidence: EvidenceSnippet | None = None
    model_version: str | None = None

    @model_validator(mode="after")
    def certainty_requires_attribution(self) -> NewsEntityMention:
        """Only the source's own attribution may be certain.

        A dictionary match on 「台積」 is strong and still an inference — the
        string occurs inside other names and beside prices. A confidence of
        exactly 1 from an inference is a number nothing downstream can discount,
        so the type refuses to represent it.
        """
        attributed = self.extraction_method is ExtractionMethod.SOURCE_ATTRIBUTED
        if self.confidence == 1 and not attributed:
            raise ValueError(
                f"confidence 1.0 requires extraction_method=SOURCE_ATTRIBUTED, "
                f"not {self.extraction_method}"
            )
        if not attributed and self.evidence is None:
            raise ValueError("an inferred mention must carry the evidence it was inferred from")
        return self


class NewsStockLink(_Base):
    """The claim that a story is about a company, with the reason for it.

    `evidence` is required and must be non-empty. A link that cannot show its
    working is one nobody can disagree with, and an inference nobody can
    disagree with is how a supply-chain hop becomes a fact.
    """

    symbol: Symbol
    market: Market = Market.TWSE

    relation_type: RelationType
    hop_count: int = Field(default=0, ge=0, le=5)

    impact_score: Ratio = Field(ge=0, le=1)
    impact_direction: int = Field(ge=-1, le=1)
    confidence: Ratio = Field(ge=0, le=1)
    sentiment: Ratio | None = Field(default=None, ge=-1, le=1)
    time_horizon: TimeHorizon = TimeHorizon.SHORT

    evidence: list[EvidenceSnippet] = Field(min_length=1)
    #: The path through the supply chain, where the link was propagated rather
    #: than observed. Empty for a direct mention.
    path: list[str] = Field(default_factory=list)
    rule: str = Field(min_length=1)

    provenance: Provenance

    @model_validator(mode="after")
    def direct_means_zero_hops(self) -> NewsStockLink:
        """A story naming a company is not a story about its supplier.

        Without this, propagation is invisible: a second-hop inference stored as
        DIRECT looks exactly like the company being named, and the difference is
        the entire basis for discounting it.
        """
        mismatched = (self.relation_type is RelationType.DIRECT) != (self.hop_count == 0)
        if mismatched and self.relation_type is not RelationType.MACRO:
            raise ValueError(
                f"{self.relation_type} with hop_count={self.hop_count}: DIRECT means "
                f"zero hops and zero hops means DIRECT"
            )
        if self.hop_count > 0 and not self.path:
            raise ValueError("a propagated link must record the path it travelled")
        return self


class NewsEventTag(_Base):
    """What kind of event a story reports, and when it happened.

    `event_time` is separate from the document's `published_at` because they are
    different facts: an exchange announcement routinely reports something that
    occurred days earlier. A study of what the market did on the news uses the
    publication time; a study of what the company did uses the event time.
    """

    event_type: str = Field(min_length=1, max_length=40)
    event_subtype: str | None = None
    severity: Ratio | None = Field(default=None, ge=0, le=1)
    confidence: Ratio = Field(ge=0, le=1)
    event_time: datetime | None = None
    classification_method: ExtractionMethod
    provenance: Provenance


class InstitutionalFlow(_Base):
    symbol: Symbol
    market: Market
    trading_date: date

    foreign_net: int | None = None
    foreign_dealer_net: int | None = None
    trust_net: int | None = None
    dealer_self_net: int | None = None
    dealer_hedge_net: int | None = None
    dealer_net: int | None = None
    total_net: int | None = None

    foreign_net_value: Money | None = None
    total_net_value: Money | None = None
    net_to_volume_pct: Ratio | None = None

    source: DataSource
    is_demo: bool = False


# ------------------------------------------------------------------ derived
class FactorScore(_Base):
    symbol: Symbol
    trading_date: date
    factor_name: str

    raw_value: Decimal | None = None
    zscore: Decimal | None = None
    percentile: Ratio | None = Field(default=None, ge=0, le=1)
    sector_zscore: Decimal | None = None

    provenance: Provenance


class ScoreContribution(_Base):
    """One line of the "why is it 91?" explanation."""

    component: str
    label_zh: str
    contribution: Decimal = Field(description="Signed; contributions + baseline == total")
    weight: Decimal
    raw_value: Decimal | None = None
    percentile: Ratio | None = None
    evidence: dict[str, object] = Field(default_factory=dict)


class AIStockScore(_Base):
    symbol: Symbol
    trading_date: date

    total_score: Decimal = Field(ge=0, le=100)
    baseline: Decimal = Decimal("50")

    technical_score: Decimal | None = None
    fundamental_score: Decimal | None = None
    institutional_score: Decimal | None = None
    momentum_score: Decimal | None = None
    news_score: Decimal | None = None
    sentiment_score: Decimal | None = None
    industry_score: Decimal | None = None
    ai_trend_score: Decimal | None = None
    valuation_score: Decimal | None = None
    risk_score: Decimal | None = None
    anomaly_score: Decimal | None = None

    rank_overall: int | None = None
    rank_in_sector: int | None = None
    percentile: Ratio | None = None
    confidence: Ratio | None = None

    contributions: list[ScoreContribution] = Field(default_factory=list)
    provenance: Provenance

    disclaimer: str = "本分數為模型推論結果，非投資建議，不代表未來績效。"

    def contributions_balance(self, tolerance: Decimal = Decimal("0.01")) -> bool:
        """Invariant INV-1: baseline + Σ contributions == total_score."""
        if not self.contributions:
            return True
        total = self.baseline + sum((c.contribution for c in self.contributions), Decimal(0))
        return abs(total - self.total_score) <= tolerance


__all__ = [
    "AIStockScore",
    "DataSource",
    "EvidenceSnippet",
    "ExtractionMethod",
    "FactorScore",
    "FinancialFact",
    "HistoricalPrice",
    "InstitutionalFlow",
    "Market",
    "MarketQuote",
    "NewsDocument",
    "NewsEntityMention",
    "NewsEventTag",
    "NewsStockLink",
    "Provenance",
    "RelationType",
    "ScoreContribution",
    "StatementType",
    "Symbol",
    "TimeHorizon",
    "Timeframe",
]
