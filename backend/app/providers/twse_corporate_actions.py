"""TWSE corporate action parsers and providers.

Four endpoints, three providers, because no single one of them can drive a price
adjustment. What each can and cannot see was established by fetching them, and
is recorded in ADR-022:

* `TWT48U_ALL` 除權除息預告表 — ex-date *ahead of the fact*, decomposed into cash,
  stock ratio and subscription terms. Forward-looking snapshot; no history.
* `TWT49U` 除權除息計算結果表 — ex-date after the fact, with the exchange's own
  pre-close and reference price. Serves a full year per request. Its
  `權值+息值` column is a **combined** figure expressed in price units, so for
  anything other than a pure `息` row it cannot be decomposed.
* `TWTAUU` 減資恢復買賣參考價格 — capital reductions, 2019–2026 in one request.

Each provider declares `supported_actions` as narrowly as the data justifies.
That is the whole mechanism by which "this source found nothing" is kept
distinct from "this source cannot see that": TWT49U is not permitted to claim
coverage of stock dividends merely because it lists a `權` row it cannot
quantify.

The parsers are pure functions over already-decoded payloads, tested directly
against recorded responses. Nothing here computes an adjustment factor — that is
a domain decision and lives in a service, per ADR-022.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.models.market import CorporateActionType
from app.models.ops import Transport
from app.providers.base import (
    ProviderError,
    ProviderErrorKind,
    ProviderHealth,
    ProviderResult,
)
from app.providers.corporate_actions import CorporateActionProvider
from app.providers.http import HttpFetcher
from app.providers.normalize import ParseError, clean, parse_decimal, parse_roc_date, parse_symbol
from app.providers.rate_limiter import RateLimitConfig, RateLimiter, get_rate_limiter

log = get_logger(__name__)

MARKET = "TWSE"

OPENAPI = "https://openapi.twse.com.tw/v1"
RWD = "https://www.twse.com.tw/rwd/zh"

FORECAST_ENDPOINT = f"{OPENAPI}/exchangeReport/TWT48U_ALL"
RESULTS_ENDPOINT = f"{RWD}/exRight/TWT49U"
# TWSE's own spelling. Not a typo on our side; `reduction` 404s.
REDUCTION_ENDPOINT = f"{RWD}/reducation/TWTAUU"

#: Values that appear in numeric fields and are not numbers. `尚未公告` is the
#: dangerous one: it sits in `SubscriptionPricePerShare`, and coerced to a number
#: it produces a rights issue priced at zero — a 100% discount that would look
#: like an enormous adjustment.
NOT_YET_ANNOUNCED = "尚未公告"
NULL_TOKENS = frozenset({"", "--", "－", "N/A", "n/a", NOT_YET_ANNOUNCED})


def _num(value: object) -> Decimal | None:
    """Numeric fields here use several spellings of nothing. All become None."""
    text = clean(value)
    if text is None or text in NULL_TOKENS:
        return None
    return parse_decimal(text, field="amount")


def _positive(value: Decimal | None) -> Decimal | None:
    """Treat an explicit zero as absent.

    These endpoints use `''` and `'0'` interchangeably for "this component did
    not occur". Keeping a zero would create a cash dividend of nothing, which
    then has to be filtered out by every consumer instead of once, here.
    """
    return value if value is not None and value != 0 else None


# ------------------------------------------------------------------ TWT48U
def parse_forecast(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """除權除息預告表 → typed records, one per component of each event.

    A `權息` row is two economic events sharing an ex-date, so it becomes two
    records. They are separable here — unlike in TWT49U — because this endpoint
    publishes the components rather than their combined price effect.
    """
    if not isinstance(payload, list):
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            f"TWT48U_ALL: expected a list, got {type(payload).__name__}",
            source=MARKET,
        )

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for row in payload:
        try:
            symbol = parse_symbol(row["Code"], field="Code")
            ex_date = parse_roc_date(row["Date"], field="Date")
            base: dict[str, Any] = {
                "symbol": symbol,
                "market": MARKET,
                "ex_date": ex_date,
                # This table is a forecast: it is published before the ex-date,
                # so its existence is itself the evidence that the market knew.
                # The precise announcement date belongs to t187ap45_L; until
                # that is joined in, knowledge time is marked as estimated
                # rather than silently equated with the ex-date.
                "announced_at": datetime.combine(ex_date, datetime.min.time(), tzinfo=UTC),
                "announced_at_is_estimated": True,
                "raw": dict(row),
            }

            cash = _positive(_num(row.get("CashDividend")))
            stock = _positive(_num(row.get("StockDividendRatio")))
            sub_ratio = _positive(_num(row.get("SubscriptionRatio")))
            sub_price_raw = clean(row.get("SubscriptionPricePerShare"))
            sub_price = _positive(_num(sub_price_raw))

            if cash is not None:
                records.append(
                    {
                        **base,
                        "action_type": CorporateActionType.CASH_DIVIDEND,
                        "cash_dividend": cash,
                    }
                )
            if stock is not None:
                records.append(
                    {
                        **base,
                        "action_type": CorporateActionType.STOCK_DIVIDEND,
                        "stock_dividend": stock,
                    }
                )
            if sub_ratio is not None:
                records.append(
                    {
                        **base,
                        "action_type": CorporateActionType.RIGHTS_ISSUE,
                        "subscription_ratio": sub_ratio,
                        "subscription_price": sub_price,
                        # A rights issue whose price the company has not yet
                        # published is a real, known event with one unknown
                        # term. Recording it with the unknown flagged is the
                        # only honest option: dropping it hides an adjustment,
                        # and defaulting the price to zero invents a 100%
                        # discount.
                        "subscription_price_pending": sub_price_raw == NOT_YET_ANNOUNCED,
                    }
                )

            if cash is None and stock is None and sub_ratio is None:
                errors.append(
                    {"row": row, "error": "row carries no cash, stock or subscription component"}
                )
        except (ParseError, KeyError, TypeError) as exc:
            errors.append({"row": row, "error": str(exc)})

    return records, errors


# ------------------------------------------------------------------- TWT49U
def parse_results(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """除權除息計算結果表 → cash dividends, plus reference prices for everything.

    Only `息` rows become actions. For `權` and `權息` the published
    `權值+息值` is a single number in price units covering a share ratio, a
    subscription, or both — it cannot be decomposed, and guessing which
    component it represents would put a fabricated ratio into the adjustment
    chain. Those rows still yield their reference prices, which is what this
    endpoint is uniquely good for.
    """
    if not isinstance(payload, dict) or "fields" not in payload:
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            "TWT49U: response is not the expected {stat, fields, data} envelope",
            source=MARKET,
        )
    if payload.get("stat") != "OK":
        raise ProviderError(
            ProviderErrorKind.NO_DATA,
            f"TWT49U: stat={payload.get('stat')!r}",
            source=MARKET,
        )

    idx = {name: n for n, name in enumerate(payload["fields"])}
    required = ("資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "權值+息值", "權/息")
    missing = [c for c in required if c not in idx]
    if missing:
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            f"TWT49U: columns missing: {missing}",
            source=MARKET,
        )

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for row in payload.get("data") or []:
        try:
            kind = clean(row[idx["權/息"]])
            ex_date = parse_roc_date(row[idx["資料日期"]], field="資料日期")
            record: dict[str, Any] = {
                "symbol": parse_symbol(row[idx["股票代號"]], field="股票代號"),
                "market": MARKET,
                "ex_date": ex_date,
                "announced_at": datetime.combine(ex_date, datetime.min.time(), tzinfo=UTC),
                "announced_at_is_estimated": True,
                "reference_price_before": _num(row[idx["除權息前收盤價"]]),
                "reference_price_after": _num(row[idx["除權息參考價"]]),
                "raw": dict(zip(payload["fields"], row, strict=False)),
            }

            if kind == "息":
                record["action_type"] = CorporateActionType.CASH_DIVIDEND
                record["cash_dividend"] = _num(row[idx["權值+息值"]])
                records.append(record)
            else:
                # Not an error and not a record. The event is real, this source
                # simply cannot say what it consisted of.
                errors.append(
                    {
                        "row": record["raw"],
                        "error": (
                            f"權/息={kind!r}: 權值+息值 is a combined price effect and "
                            "cannot be decomposed from this endpoint — see TWT48U_ALL"
                        ),
                        "kind": "undecomposable",
                    }
                )
        except (ParseError, KeyError, IndexError, TypeError) as exc:
            errors.append({"row": row, "error": str(exc)})

    return records, errors


# ------------------------------------------------------------------ TWTAUU
#: 減資原因 → whether shareholders got money back.
REDUCTION_REASONS: dict[str, bool] = {
    "退還股款": True,  # cash returned and shares cancelled
    "彌補虧損": False,  # shares cancelled to absorb losses; no cash
}


def parse_reductions(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """股票減資恢復買賣參考價格 → capital reductions.

    `cash_returned_per_share` is deliberately left unset even for 退還股款. The
    only observable here is one price change, and it reflects both the cash
    returned and the share ratio — one equation, two unknowns. Back-solving it
    would produce a number with no source, which is the thing this platform is
    built not to do.
    """
    if not isinstance(payload, dict) or "fields" not in payload:
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            "TWTAUU: response is not the expected {stat, fields, data} envelope",
            source=MARKET,
        )

    idx = {name: n for n, name in enumerate(payload["fields"])}
    required = ("恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "減資原因")
    missing = [c for c in required if c not in idx]
    if missing:
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED, f"TWTAUU: columns missing: {missing}", source=MARKET
        )

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for row in payload.get("data") or []:
        try:
            reason = clean(row[idx["減資原因"]]) or ""
            if reason not in REDUCTION_REASONS:
                errors.append({"row": row, "error": f"unknown 減資原因 {reason!r}"})
                continue

            resumption = parse_roc_date(row[idx["恢復買賣日期"]], field="恢復買賣日期")
            records.append(
                {
                    "symbol": parse_symbol(row[idx["股票代號"]], field="股票代號"),
                    "market": MARKET,
                    "action_type": CorporateActionType.CAPITAL_REDUCTION,
                    # The resumption date is when the adjusted price first
                    # trades, which is the date a price series has to be
                    # adjusted on. The suspension began earlier; that is in
                    # `raw` rather than being mistaken for the ex-date.
                    "ex_date": resumption,
                    "announced_at": datetime.combine(resumption, datetime.min.time(), tzinfo=UTC),
                    "announced_at_is_estimated": True,
                    "reference_price_before": _num(row[idx["停止買賣前收盤價格"]]),
                    "reference_price_after": _num(row[idx["恢復買賣參考價"]]),
                    "reduction_returns_cash": REDUCTION_REASONS[reason],
                    "reduction_reason": reason,
                    "raw": dict(zip(payload["fields"], row, strict=False)),
                }
            )
        except (ParseError, KeyError, IndexError, TypeError) as exc:
            errors.append({"row": row, "error": str(exc)})

    return records, errors


# ------------------------------------------------------------------ providers
class _TWSEBase(CorporateActionProvider):
    """Shared transport. Subclasses differ only in endpoint and capability."""

    dataset = "corporate_actions"
    probe_endpoint = FORECAST_ENDPOINT

    def __init__(
        self,
        *,
        limiter: RateLimiter | None = None,
        rate_limit: RateLimitConfig | None = None,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        fetcher: HttpFetcher | None = None,
    ) -> None:
        # Same limiter and the same source key as the price provider, so the
        # exchange sees one budget for this application rather than one per
        # class of data. Corporate action backfill runs alongside daily
        # ingestion; separate budgets would double the rate we hit TWSE at
        # exactly the moment we are already busiest.
        cfg = rate_limit or RateLimitConfig(
            source=MARKET, requests_per_minute=60, max_concurrency=2, min_interval_ms=350
        )
        self._fetcher = fetcher or HttpFetcher(
            source=MARKET,
            base_url=OPENAPI,
            rate_limit=cfg,
            limiter=limiter or get_rate_limiter(),
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )

    async def health(self) -> ProviderHealth:
        started = datetime.now(UTC)
        try:
            await self._fetcher.fetch_json(self.probe_endpoint, dataset=self.dataset)
        except ProviderError as exc:
            return ProviderHealth(
                source=self.code, reachable=False, error=str(exc), checked_at=started
            )
        return ProviderHealth(
            source=self.code,
            reachable=True,
            checked_at=started,
            latency_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
        )

    async def aclose(self) -> None:
        await self._fetcher.aclose()

    @staticmethod
    def _within(records: list[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
        return [r for r in records if start <= r["ex_date"] <= end]

    @staticmethod
    def _for_symbol(records: list[dict[str, Any]], symbol: str | None) -> list[dict[str, Any]]:
        return records if symbol is None else [r for r in records if r["symbol"] == symbol]


class TWSEForecastProvider(_TWSEBase):
    """除權除息預告表. Decomposed, forward-looking, no history."""

    code = "TWSE_EXRIGHT_FORECAST"
    market = MARKET
    transport = Transport.LIVE
    supported_actions = frozenset(
        {
            CorporateActionType.CASH_DIVIDEND,
            CorporateActionType.STOCK_DIVIDEND,
            CorporateActionType.RIGHTS_ISSUE,
        }
    )

    async def get_corporate_actions(
        self, *, start: date, end: date, symbol: str | None = None
    ) -> ProviderResult:
        payload, meta = await self._fetcher.fetch_json(FORECAST_ENDPOINT, dataset=self.dataset)
        records, errors = parse_forecast(payload)
        meta.dataset = self.dataset
        meta.source_endpoint = FORECAST_ENDPOINT
        meta.data_as_of = end
        return ProviderResult(
            records=self._for_symbol(self._within(records, start, end), symbol),
            metadata=meta,
            parse_errors=errors,
            raw_payload=payload,
        )


class TWSEResultsProvider(_TWSEBase):
    """除權除息計算結果表. Cash dividends only — see `parse_results`."""

    code = "TWSE_EXRIGHT_RESULTS"
    market = MARKET
    transport = Transport.LIVE
    supported_actions = frozenset({CorporateActionType.CASH_DIVIDEND})
    history_starts = date(2019, 1, 1)

    async def get_corporate_actions(
        self, *, start: date, end: date, symbol: str | None = None
    ) -> ProviderResult:
        payload, meta = await self._fetcher.fetch_json(
            RESULTS_ENDPOINT,
            dataset=self.dataset,
            params={
                "startDate": start.strftime("%Y%m%d"),
                "endDate": end.strftime("%Y%m%d"),
                "response": "json",
            },
        )
        records, errors = parse_results(payload)
        meta.dataset = self.dataset
        meta.source_endpoint = RESULTS_ENDPOINT
        meta.data_as_of = end
        return ProviderResult(
            records=self._for_symbol(records, symbol),
            metadata=meta,
            parse_errors=errors,
            raw_payload=payload,
        )


class TWSEReductionProvider(_TWSEBase):
    """股票減資恢復買賣參考價格."""

    code = "TWSE_CAPITAL_REDUCTION"
    market = MARKET
    transport = Transport.LIVE
    supported_actions = frozenset({CorporateActionType.CAPITAL_REDUCTION})
    history_starts = date(2019, 1, 1)

    async def get_corporate_actions(
        self, *, start: date, end: date, symbol: str | None = None
    ) -> ProviderResult:
        payload, meta = await self._fetcher.fetch_json(
            REDUCTION_ENDPOINT,
            dataset=self.dataset,
            params={
                "startDate": start.strftime("%Y%m%d"),
                "endDate": end.strftime("%Y%m%d"),
                "response": "json",
            },
        )
        records, errors = parse_reductions(payload)
        meta.dataset = self.dataset
        meta.source_endpoint = REDUCTION_ENDPOINT
        meta.data_as_of = end
        return ProviderResult(
            records=self._for_symbol(records, symbol),
            metadata=meta,
            parse_errors=errors,
            raw_payload=payload,
        )


__all__ = [
    "TWSEForecastProvider",
    "TWSEReductionProvider",
    "TWSEResultsProvider",
    "parse_forecast",
    "parse_reductions",
    "parse_results",
]
