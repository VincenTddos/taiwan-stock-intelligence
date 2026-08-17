"""Corporate action provider abstraction.

Separate from `BaseMarketDataProvider` on purpose. The source that publishes
prices is not necessarily the source that publishes dividends: TWSE serves
ex-rights and ex-dividend results, MOPS carries board resolutions earlier and in
more detail, and a licensed vendor would supply both plus the ones neither of
those covers. Folding this into the price provider would make "who gives us
prices" and "who gives us corporate actions" the same decision forever.

Nothing below names an exchange or an endpoint. A concrete provider is selected
from the `data_sources` registry the same way price providers are, so the domain
and service layers state what they need — actions for a symbol over a date
range — and never learn where it came from.

Two boundaries this file draws deliberately:

**Providers report economic facts, not adjustment factors.** A provider says
"NT$2.50 cash and 0.05 shares per share, ex on this date". It does not say what
that does to a price series. The adjustment formula is a domain decision, it is
market-specific, and more than one convention is defensible — so it lives in a
service where it can be tested and changed, not smeared across every provider
that has to reimplement it.

**A provider declares which action types it can see.** Without that, "no rights
issue found" and "this source has never carried rights issues" are the same
answer, and a return series silently loses a real adjustment. `supported_actions`
makes the second case a coverage fact the caller can check.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Any, NotRequired, TypedDict

from app.models.market import CorporateActionType
from app.models.ops import Transport
from app.providers.base import (
    Capability,
    ProviderError,
    ProviderErrorKind,
    ProviderHealth,
    ProviderResult,
)


class CorporateActionRecord(TypedDict):
    """The canonical shape a corporate action provider must produce.

    Every field beyond the first four is optional because no single action uses
    all of them: a cash dividend has no subscription price, a loss-offsetting
    capital reduction returns no cash. What must always be present is the
    identity of the action and the two dates that make it usable in a
    point-in-time query.
    """

    symbol: str
    market: str
    action_type: CorporateActionType
    ex_date: date

    # Knowledge time. Required for point-in-time correctness — an adjustment may
    # only be applied to a backtest that has reached the date the market could
    # have known about it. When a source publishes only the ex-date, the
    # ingestion layer records an estimate and flags it; a provider must not
    # quietly substitute the ex-date and call it knowledge.
    announced_at: NotRequired[Any]
    announced_at_is_estimated: NotRequired[bool]

    record_date: NotRequired[date | None]
    payment_date: NotRequired[date | None]

    # Per-share economics. Interpretation is fixed by action_type.
    cash_dividend: NotRequired[Decimal | None]
    stock_dividend: NotRequired[Decimal | None]
    cash_returned_per_share: NotRequired[Decimal | None]
    split_ratio: NotRequired[Decimal | None]
    subscription_price: NotRequired[Decimal | None]
    subscription_ratio: NotRequired[Decimal | None]

    # What the exchange itself published as the reference price either side of
    # the event, where available. Not used to compute the adjustment — used to
    # check it. A factor derived independently and then reconciled against the
    # exchange's own number is a factor that can be shown to be right.
    reference_price_before: NotRequired[Decimal | None]
    reference_price_after: NotRequired[Decimal | None]

    raw: NotRequired[dict[str, Any]]


# Which per-share fields each action type is expected to carry. Used by the
# ingestion layer to reject a record that claims to be one thing and carries the
# numbers of another — a stock dividend with only a subscription price is a
# parser bug, and it should surface as one rather than as a silent zero
# adjustment.
EXPECTED_FIELDS: dict[CorporateActionType, frozenset[str]] = {
    CorporateActionType.CASH_DIVIDEND: frozenset({"cash_dividend"}),
    CorporateActionType.STOCK_DIVIDEND: frozenset({"stock_dividend"}),
    CorporateActionType.SPLIT: frozenset({"split_ratio"}),
    CorporateActionType.REVERSE_SPLIT: frozenset({"split_ratio"}),
    CorporateActionType.RIGHTS_ISSUE: frozenset({"subscription_price", "subscription_ratio"}),
    # Cash reductions return money and cancel shares; loss-offsetting reductions
    # only cancel shares. So the ratio is required and the cash is not.
    CorporateActionType.CAPITAL_REDUCTION: frozenset({"split_ratio"}),
    CorporateActionType.PAR_VALUE_CHANGE: frozenset({"split_ratio"}),
}


class CorporateActionProvider(ABC):
    """Contract every corporate action source implements.

    Mirrors `BaseMarketDataProvider`: same error taxonomy, same `ProviderResult`
    envelope, same rule that an unsupported request raises rather than returning
    empty. The pipeline downstream of this — provenance, validation, quarantine,
    idempotent upsert — is the one that already exists, unchanged.
    """

    code: str = "BASE"
    market: str = ""
    transport: Transport = Transport.LIVE

    #: Action types this source is capable of reporting. A type absent from this
    #: set is not "none occurred" — it is "this source cannot tell you".
    supported_actions: frozenset[CorporateActionType] = frozenset()

    #: Earliest date the source can serve, if it is known and bounded. `None`
    #: means unknown, which is not the same as unlimited and should be treated
    #: as a coverage gap until someone establishes otherwise.
    history_starts: date | None = None

    capabilities: frozenset[Capability] = frozenset({Capability.CORPORATE_ACTIONS})

    def supports(self, action_type: CorporateActionType) -> bool:
        return action_type in self.supported_actions

    def _require(self, action_type: CorporateActionType) -> None:
        if not self.supports(action_type):
            raise ProviderError(
                ProviderErrorKind.NOT_SUPPORTED,
                f"{self.code} does not report {action_type}",
                source=self.code,
            )

    # ------------------------------------------------------------------
    @abstractmethod
    async def get_corporate_actions(
        self,
        *,
        start: date,
        end: date,
        symbol: str | None = None,
    ) -> ProviderResult:
        """Actions with an ex-date in [start, end], optionally for one symbol.

        The range is over **ex-date**, because that is the axis every source
        indexes on and the one the adjustment pipeline consumes. Filtering by
        announcement time is the availability service's job and is done after
        ingestion, against stored rows.

        Records must satisfy `CorporateActionRecord`. Rows that cannot be parsed
        belong in `ProviderResult.parse_errors`, never dropped: a source
        changing its layout must look like a failure, not like a quarter with no
        dividends.
        """

    @abstractmethod
    async def health(self) -> ProviderHealth:
        """Whether this source is reachable and answering."""

    async def aclose(self) -> None:
        """Release transport resources. Safe to call more than once."""
        return None


__all__ = [
    "EXPECTED_FIELDS",
    "CorporateActionProvider",
    "CorporateActionRecord",
]
