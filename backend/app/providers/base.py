"""Provider abstraction.

A provider's only job is: fetch bytes from one source, parse them into canonical
records, and report exactly what it did. It does not validate business rules, it
does not write to the database, and it does not decide whether a trading day
exists. Those belong to later pipeline stages, and keeping them out is what lets
the same canonical records come from a live fetch, a replayed recording, or a
future licensed feed without anything downstream noticing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from app.models.ops import Transport


class Capability(StrEnum):
    MARKET_STATUS = "MARKET_STATUS"
    TRADING_CALENDAR = "TRADING_CALENDAR"
    STOCK_MASTER = "STOCK_MASTER"
    DAILY_PRICES = "DAILY_PRICES"
    MARKET_INDEX = "MARKET_INDEX"
    INSTITUTIONAL_FLOW = "INSTITUTIONAL_FLOW"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"


class ProviderErrorKind(StrEnum):
    """Normalised failure taxonomy.

    Every provider maps its own exceptions onto these, so the pipeline can decide
    retry-or-not without knowing which source failed.
    """

    TIMEOUT = "TIMEOUT"
    CONNECTION = "CONNECTION"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_FOUND = "NOT_FOUND"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    SCHEMA_CHANGED = "SCHEMA_CHANGED"
    NO_DATA = "NO_DATA"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    CONFIG_ERROR = "CONFIG_ERROR"


# Which failures are worth trying again. A 404 or a schema change will not fix
# itself by being asked twice.
RETRYABLE: frozenset[ProviderErrorKind] = frozenset(
    {
        ProviderErrorKind.TIMEOUT,
        ProviderErrorKind.CONNECTION,
        ProviderErrorKind.RATE_LIMITED,
        ProviderErrorKind.UPSTREAM_ERROR,
    }
)


class ProviderError(Exception):
    def __init__(
        self,
        kind: ProviderErrorKind,
        message: str,
        *,
        source: str = "",
        endpoint: str = "",
        status_code: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.kind = kind
        self.message = message
        self.source = source
        self.endpoint = endpoint
        self.status_code = status_code
        self.__cause__ = cause
        super().__init__(f"[{source or '?'}/{kind}] {message}")

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


@dataclass(slots=True)
class SourceMetadata:
    """Provenance for one provider call. Written verbatim to `raw_ingestions`."""

    source: str
    source_endpoint: str
    dataset: str
    transport: Transport
    source_request_at: datetime
    source_response_at: datetime | None = None
    params: dict[str, Any] = field(default_factory=dict)
    http_status: int | None = None
    duration_ms: int | None = None
    response_bytes: int | None = None
    response_hash: str | None = None
    data_as_of: date | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_endpoint": self.source_endpoint,
            "dataset": self.dataset,
            "transport": str(self.transport),
            "source_request_at": self.source_request_at,
            "source_response_at": self.source_response_at,
            "params": self.params,
            "http_status": self.http_status,
            "duration_ms": self.duration_ms,
            "response_bytes": self.response_bytes,
            "response_hash": self.response_hash,
            "data_as_of": self.data_as_of,
        }


@dataclass(slots=True)
class ProviderResult:
    """What a provider returns: parsed records plus how they were obtained.

    `parse_errors` carries rows the provider could not turn into records. They are
    not dropped — the pipeline quarantines them, because a source changing its
    format must be visible rather than looking like a quiet day.
    """

    records: list[dict[str, Any]]
    metadata: SourceMetadata
    parse_errors: list[dict[str, Any]] = field(default_factory=list)
    raw_payload: Any = None

    @property
    def record_count(self) -> int:
        return len(self.records)


@dataclass(slots=True)
class ProviderHealth:
    source: str
    reachable: bool
    latency_ms: float | None = None
    checked_at: datetime | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class BaseMarketDataProvider(ABC):
    """Contract every market data source implements.

    Unsupported capabilities raise `ProviderErrorKind.NOT_SUPPORTED` rather than
    returning empty, so a missing capability is a loud configuration error and
    not a silently empty dataset.
    """

    code: str = "BASE"
    market: str = ""
    capabilities: frozenset[Capability] = frozenset()
    transport: Transport = Transport.LIVE

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def _require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise ProviderError(
                ProviderErrorKind.NOT_SUPPORTED,
                f"{self.code} does not provide {capability}",
                source=self.code,
            )

    # ------------------------------------------------------------------
    @abstractmethod
    async def get_market_status(self) -> ProviderResult:
        """Whatever the source says about its own current state."""

    @abstractmethod
    async def get_trading_calendar(self, year: int) -> ProviderResult:
        """Published holiday / session schedule for a calendar year."""

    @abstractmethod
    async def get_stock_master(self) -> ProviderResult:
        """The listed-security master file."""

    @abstractmethod
    async def get_daily_prices(
        self,
        *,
        trading_date: date | None = None,
        symbol: str | None = None,
        month: date | None = None,
    ) -> ProviderResult:
        """Daily OHLCV.

        Sources differ in how they slice this: some give the whole market for one
        day, others one symbol for one month. Callers state what they want and
        the provider raises `NOT_SUPPORTED` if it cannot serve that shape.
        """

    @abstractmethod
    async def get_market_index(self, *, trading_date: date | None = None) -> ProviderResult:
        """Index closes."""

    @abstractmethod
    async def get_institutional_flow(self, *, trading_date: date) -> ProviderResult:
        """Per-symbol institutional buy/sell for one trading day."""

    # ------------------------------------------------------------------
    async def get_corporate_actions(self, *, year: int | None = None) -> ProviderResult:
        """Optional; default implementation reports the capability as absent."""
        self._require(Capability.CORPORATE_ACTIONS)
        raise NotImplementedError

    @abstractmethod
    async def health(self) -> ProviderHealth:
        """Cheap reachability probe. Must never raise."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release transport resources. Safe to call more than once.

        Not abstract: providers with no transport (replay) have nothing to close.
        """


__all__ = [
    "RETRYABLE",
    "BaseMarketDataProvider",
    "Capability",
    "ProviderError",
    "ProviderErrorKind",
    "ProviderHealth",
    "ProviderResult",
    "SourceMetadata",
]
