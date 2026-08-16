"""Taiwan Stock Exchange provider.

Endpoint choices are driven by what was actually verified (DATA_SOURCES.md), not
by what documentation claims:

* `openapi.twse.com.tw/v1/...` — snapshot endpoints. **No date parameter**: they
  return the most recent trading day and, on a non-trading day, silently repeat
  the previous one. Every record therefore takes its trading date from the
  payload, never from the clock.
* `www.twse.com.tw/rwd/zh/...` — the only date-parameterised endpoints, and thus
  the only path for historical backfill.
* `/v1/holidaySchedule/holidaySchedule` — the authoritative published calendar.
  Note it contains entries that are *not* closures (`國曆新年開始交易日`), so it
  cannot be consumed as "every row is a holiday".

Parsing is separated from fetching so the pure functions can be tested against
recorded responses without any network.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.core.logging import get_logger
from app.models.market import SessionType
from app.models.ops import Transport
from app.providers.base import (
    BaseMarketDataProvider,
    Capability,
    ProviderError,
    ProviderErrorKind,
    ProviderHealth,
    ProviderResult,
)
from app.providers.http import HttpFetcher
from app.providers.normalize import (
    ParseError,
    clean,
    parse_change,
    parse_decimal,
    parse_int,
    parse_name,
    parse_par_value,
    parse_roc_date,
    parse_symbol,
    rows_to_dicts,
    utcnow,
)
from app.providers.rate_limiter import RateLimitConfig, RateLimiter, get_rate_limiter

log = get_logger(__name__)

OPENAPI_BASE = "https://openapi.twse.com.tw/v1"
RWD_BASE = "https://www.twse.com.tw/rwd/zh"

MARKET = "TWSE"

# Index names that are the headline market indices rather than sub-indices.
MARKET_INDEX_NAMES = {
    "發行量加權股價指數": "TAIEX",
    "未含金融指數": "TAIEX_EX_FIN",
    "未含金融電子指數": "TAIEX_EX_FIN_ELEC",
    "寶島股價指數": "FORMOSA",
}

# Holiday-schedule rows whose Name marks an *annotation*, not a closure.
# Consuming every row as a holiday would delete real trading days from the
# calendar — the market trades on 農曆春節前最後交易日.
NON_CLOSURE_MARKERS = ("開始交易", "最後交易")
SETTLEMENT_ONLY_MARKER = "僅辦理結算交割"

# TWSE industry codes (產業別 in t187ap03_L). Published classification.
INDUSTRY_NAMES: dict[str, str] = {
    "01": "水泥工業",
    "02": "食品工業",
    "03": "塑膠工業",
    "04": "紡織纖維",
    "05": "電機機械",
    "06": "電器電纜",
    "08": "玻璃陶瓷",
    "09": "造紙工業",
    "10": "鋼鐵工業",
    "11": "橡膠工業",
    "12": "汽車工業",
    "14": "建材營造",
    "15": "航運業",
    "16": "觀光餐旅",
    "17": "金融保險",
    "18": "貿易百貨",
    "19": "綜合",
    "20": "其他",
    "21": "化學工業",
    "22": "生技醫療業",
    "23": "油電燃氣業",
    "24": "半導體業",
    "25": "電腦及週邊設備業",
    "26": "光電業",
    "27": "通信網路業",
    "28": "電子零組件業",
    "29": "電子通路業",
    "30": "資訊服務業",
    "31": "其他電子業",
    "32": "文化創意業",
    "33": "農業科技業",
    "34": "電子商務",
    "35": "綠能環保",
    "36": "數位雲端",
    "37": "運動休閒",
    "38": "居家生活",
    "80": "管理股票",
}


# =====================================================================
# Pure parsers — no I/O, exercised directly by contract tests
# =====================================================================
def parse_holiday_schedule(
    payload: Any, year: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn the published schedule into calendar rows.

    Returns `(records, parse_errors)`. Only genuine closures become records;
    annotation rows are reported so the count is auditable but do not close the
    market.
    """
    if not isinstance(payload, list):
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            f"holiday schedule should be a list, got {type(payload).__name__}",
            source=MARKET,
        )

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for raw in payload:
        try:
            name = parse_name(raw.get("Name")) or ""
            day = parse_roc_date(raw.get("Date"), field="Date")
            description = clean(raw.get("Description")) or ""
            description = description.replace("<br>", " ").strip()

            if any(marker in name for marker in NON_CLOSURE_MARKERS):
                errors.append(
                    {
                        "reason": "annotation_row_not_a_closure",
                        "raw": raw,
                        "note": f"'{name}' marks a trading day, not a closure",
                    }
                )
                continue

            session = (
                SessionType.SETTLEMENT_ONLY
                if SETTLEMENT_ONLY_MARKER in name
                else SessionType.CLOSED
            )
            records.append(
                {
                    "market": MARKET,
                    "calendar_date": day,
                    "is_trading_day": False,
                    "session_type": str(session),
                    "holiday_name": name,
                    "description": description or None,
                    "source": MARKET,
                }
            )
        except (ParseError, AttributeError) as exc:
            errors.append({"reason": str(exc), "raw": raw})

    return records, errors


def parse_stock_master(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse `t187ap03_L` into stock master records."""
    if not isinstance(payload, list):
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            f"stock master should be a list, got {type(payload).__name__}",
            source=MARKET,
        )

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for raw in payload:
        try:
            symbol = parse_symbol(raw.get("公司代號"), field="公司代號")
            industry_code = clean(raw.get("產業別"))
            listing_raw = clean(raw.get("上市日期"))

            records.append(
                {
                    "symbol": symbol,
                    "market": MARKET,
                    "name": parse_name(raw.get("公司名稱")) or symbol,
                    "short_name": parse_name(raw.get("公司簡稱")),
                    "short_name_en": parse_name(raw.get("英文簡稱")),
                    "security_type": "COMMON",
                    "industry_code": industry_code,
                    "industry_name": INDUSTRY_NAMES.get(industry_code or ""),
                    "listing_date": parse_roc_date(listing_raw, field="上市日期")
                    if listing_raw
                    else None,
                    "status": "LISTED",
                    "par_value": parse_par_value(raw.get("普通股每股面額")),
                    "paid_in_capital": parse_decimal(raw.get("實收資本額"), field="實收資本額"),
                    "shares_outstanding": parse_int(
                        raw.get("已發行普通股數或TDR原股發行股數"), field="已發行普通股數"
                    ),
                    "tax_id": clean(raw.get("營利事業統一編號")),
                    "attributes": {
                        "chairman": parse_name(raw.get("董事長")),
                        "ceo": parse_name(raw.get("總經理")),
                        "spokesperson": parse_name(raw.get("發言人")),
                        "website": clean(raw.get("網址")),
                        "address": parse_name(raw.get("住址")),
                        "established_date": clean(raw.get("成立日期")),
                    },
                    "source": MARKET,
                }
            )
        except (ParseError, AttributeError) as exc:
            errors.append({"reason": str(exc), "raw": raw})

    return records, errors


def parse_stock_day_all(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse the whole-market snapshot (`STOCK_DAY_ALL`)."""
    if not isinstance(payload, list):
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            f"STOCK_DAY_ALL should be a list, got {type(payload).__name__}",
            source=MARKET,
        )

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for raw in payload:
        try:
            change, comparable = parse_change(raw.get("Change"), field="Change")
            records.append(
                {
                    "symbol": parse_symbol(raw.get("Code"), field="Code"),
                    "market": MARKET,
                    # From the payload. Using today() here is the bug this
                    # entire design exists to prevent.
                    "trading_date": parse_roc_date(raw.get("Date"), field="Date"),
                    "name": parse_name(raw.get("Name")),
                    "open": parse_decimal(raw.get("OpeningPrice"), field="OpeningPrice"),
                    "high": parse_decimal(raw.get("HighestPrice"), field="HighestPrice"),
                    "low": parse_decimal(raw.get("LowestPrice"), field="LowestPrice"),
                    "close": parse_decimal(raw.get("ClosingPrice"), field="ClosingPrice"),
                    "change": change,
                    "volume": parse_int(raw.get("TradeVolume"), field="TradeVolume"),
                    "turnover": parse_decimal(raw.get("TradeValue"), field="TradeValue"),
                    "trade_count": parse_int(raw.get("Transaction"), field="Transaction"),
                    "note": None if comparable else "NOT_COMPARABLE",
                    "source": MARKET,
                }
            )
        except (ParseError, AttributeError) as exc:
            errors.append({"reason": str(exc), "raw": raw})

    return records, errors


def parse_stock_day(payload: Any, symbol: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse one symbol's month of daily bars (`STOCK_DAY`).

    `stat` carries the source's own verdict; anything other than OK means there
    is no data (a non-trading month, a delisted symbol), which is reported as
    NO_DATA rather than being mistaken for an empty but valid month.
    """
    if not isinstance(payload, dict):
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            f"STOCK_DAY should be an object, got {type(payload).__name__}",
            source=MARKET,
        )

    stat = clean(payload.get("stat"))
    if stat and stat.upper() != "OK":
        raise ProviderError(ProviderErrorKind.NO_DATA, f"stat={stat}", source=MARKET)

    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    if not fields:
        raise ProviderError(ProviderErrorKind.SCHEMA_CHANGED, "missing 'fields'", source=MARKET)

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for raw in rows_to_dicts(list(fields), list(rows)):
        try:
            change, comparable = parse_change(raw.get("漲跌價差"), field="漲跌價差")
            note = clean(raw.get("註記"))
            records.append(
                {
                    "symbol": symbol,
                    "market": MARKET,
                    "trading_date": parse_roc_date(raw.get("日期"), field="日期"),
                    "open": parse_decimal(raw.get("開盤價"), field="開盤價"),
                    "high": parse_decimal(raw.get("最高價"), field="最高價"),
                    "low": parse_decimal(raw.get("最低價"), field="最低價"),
                    "close": parse_decimal(raw.get("收盤價"), field="收盤價"),
                    "change": change,
                    "volume": parse_int(raw.get("成交股數"), field="成交股數"),
                    "turnover": parse_decimal(raw.get("成交金額"), field="成交金額"),
                    "trade_count": parse_int(raw.get("成交筆數"), field="成交筆數"),
                    # '**' flags a face-value change — a corporate action the
                    # price series must be adjusted for.
                    "note": note if note else (None if comparable else "NOT_COMPARABLE"),
                    "source": MARKET,
                }
            )
        except ParseError as exc:
            errors.append({"reason": str(exc), "raw": raw})

    return records, errors


def parse_index_quotes(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse `MI_INDEX`. The sign lives in `漲跌`, not in `漲跌點數`."""
    if not isinstance(payload, list):
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            f"MI_INDEX should be a list, got {type(payload).__name__}",
            source=MARKET,
        )

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for raw in payload:
        try:
            name = parse_name(raw.get("指數")) or ""
            if not name:
                raise ParseError("指數", raw.get("指數"), "empty index name")

            code = MARKET_INDEX_NAMES.get(name, name)
            change, comparable = parse_change(
                raw.get("漲跌點數"), sign=raw.get("漲跌"), field="漲跌點數"
            )
            pct, _ = parse_change(raw.get("漲跌百分比"), field="漲跌百分比")

            records.append(
                {
                    "index_code": code,
                    "index_name": name,
                    "market": MARKET,
                    "index_type": "MARKET" if name in MARKET_INDEX_NAMES else "INDUSTRY",
                    "trading_date": parse_roc_date(raw.get("日期"), field="日期"),
                    "close": parse_decimal(raw.get("收盤指數"), field="收盤指數", allow_null=False),
                    "change": change,
                    "change_pct": pct,
                    "source": MARKET,
                    "comparable": comparable,
                }
            )
        except ParseError as exc:
            errors.append({"reason": str(exc), "raw": raw})

    return records, errors


# T86 column -> (investor_type, measure). Buy and sell are kept separately;
# storing only the net would discard turnover.
_T86_COLUMNS: dict[str, tuple[str, str]] = {
    "外陸資買進股數(不含外資自營商)": ("FOREIGN", "buy_volume"),
    "外陸資賣出股數(不含外資自營商)": ("FOREIGN", "sell_volume"),
    "外陸資買賣超股數(不含外資自營商)": ("FOREIGN", "net_volume"),
    "外資自營商買進股數": ("FOREIGN_DEALER", "buy_volume"),
    "外資自營商賣出股數": ("FOREIGN_DEALER", "sell_volume"),
    "外資自營商買賣超股數": ("FOREIGN_DEALER", "net_volume"),
    "投信買進股數": ("INVESTMENT_TRUST", "buy_volume"),
    "投信賣出股數": ("INVESTMENT_TRUST", "sell_volume"),
    "投信買賣超股數": ("INVESTMENT_TRUST", "net_volume"),
    "自營商買進股數(自行買賣)": ("DEALER_SELF", "buy_volume"),
    "自營商賣出股數(自行買賣)": ("DEALER_SELF", "sell_volume"),
    "自營商買賣超股數(自行買賣)": ("DEALER_SELF", "net_volume"),
    "自營商買進股數(避險)": ("DEALER_HEDGE", "buy_volume"),
    "自營商賣出股數(避險)": ("DEALER_HEDGE", "sell_volume"),
    "自營商買賣超股數(避險)": ("DEALER_HEDGE", "net_volume"),
    "自營商買賣超股數": ("DEALER", "net_volume"),
    "三大法人買賣超股數": ("TOTAL", "net_volume"),
}


def parse_institutional_flow(
    payload: Any, trading_date: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse `T86` into one record per (symbol, investor_type)."""
    if not isinstance(payload, dict):
        raise ProviderError(
            ProviderErrorKind.SCHEMA_CHANGED,
            f"T86 should be an object, got {type(payload).__name__}",
            source=MARKET,
        )

    stat = clean(payload.get("stat"))
    if stat and stat.upper() != "OK":
        raise ProviderError(ProviderErrorKind.NO_DATA, f"stat={stat}", source=MARKET)

    fields = list(payload.get("fields") or [])
    rows = list(payload.get("data") or [])
    if not fields:
        raise ProviderError(ProviderErrorKind.SCHEMA_CHANGED, "missing 'fields'", source=MARKET)

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for raw in rows_to_dicts(fields, rows):
        try:
            symbol = parse_symbol(raw.get("證券代號"), field="證券代號")
            by_investor: dict[str, dict[str, Any]] = {}

            for column, (investor, measure) in _T86_COLUMNS.items():
                if column not in raw:
                    continue
                slot = by_investor.setdefault(
                    investor,
                    {
                        "symbol": symbol,
                        "market": MARKET,
                        "trading_date": trading_date,
                        "investor_type": investor,
                        "source": MARKET,
                    },
                )
                slot[measure] = parse_int(raw.get(column), field=column)

            for rec in by_investor.values():
                buy, sell, net = (
                    rec.get("buy_volume"),
                    rec.get("sell_volume"),
                    rec.get("net_volume"),
                )
                # Derive whichever leg the source omitted, but never invent one
                # when two of the three are missing.
                if net is None and buy is not None and sell is not None:
                    rec["net_volume"] = buy - sell
                records.append(rec)

        except ParseError as exc:
            errors.append({"reason": str(exc), "raw": raw})

    return records, errors


# =====================================================================
class TWSEProvider(BaseMarketDataProvider):
    code = MARKET
    market = MARKET
    capabilities = frozenset(
        {
            Capability.MARKET_STATUS,
            Capability.TRADING_CALENDAR,
            Capability.STOCK_MASTER,
            Capability.DAILY_PRICES,
            Capability.MARKET_INDEX,
            Capability.INSTITUTIONAL_FLOW,
        }
    )
    transport = Transport.LIVE

    def __init__(
        self,
        *,
        limiter: RateLimiter | None = None,
        rate_limit: RateLimitConfig | None = None,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        cfg = rate_limit or RateLimitConfig(
            source=MARKET, requests_per_minute=60, max_concurrency=2, min_interval_ms=350
        )
        self._openapi = HttpFetcher(
            source=MARKET,
            base_url=OPENAPI_BASE,
            rate_limit=cfg,
            limiter=limiter or get_rate_limiter(),
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        self._rwd = HttpFetcher(
            source=MARKET,
            base_url=RWD_BASE,
            rate_limit=cfg,
            limiter=limiter or get_rate_limiter(),
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )

    async def aclose(self) -> None:
        await self._openapi.aclose()
        await self._rwd.aclose()

    # ------------------------------------------------------------------
    async def get_market_status(self) -> ProviderResult:
        payload, meta = await self._openapi.fetch_json(
            "/exchangeReport/MI_INDEX", dataset="market_status"
        )
        records, errors = parse_index_quotes(payload)
        as_of = records[0]["trading_date"] if records else None
        meta.data_as_of = as_of
        return ProviderResult(
            records=[{"market": MARKET, "last_trading_date": as_of, "index_count": len(records)}],
            metadata=meta,
            parse_errors=errors,
            raw_payload=payload,
        )

    async def get_trading_calendar(self, year: int) -> ProviderResult:
        payload, meta = await self._openapi.fetch_json(
            "/holidaySchedule/holidaySchedule", dataset="trading_calendar"
        )
        records, errors = parse_holiday_schedule(payload, year)
        return ProviderResult(records, meta, errors, raw_payload=payload)

    async def get_stock_master(self) -> ProviderResult:
        payload, meta = await self._openapi.fetch_json(
            "/opendata/t187ap03_L", dataset="stock_master"
        )
        records, errors = parse_stock_master(payload)
        return ProviderResult(records, meta, errors, raw_payload=payload)

    async def get_daily_prices(
        self,
        *,
        trading_date: date | None = None,
        symbol: str | None = None,
        month: date | None = None,
    ) -> ProviderResult:
        # One symbol, one month — the only historical path.
        if symbol is not None:
            target = month or trading_date
            if target is None:
                raise ProviderError(
                    ProviderErrorKind.CONFIG_ERROR,
                    "per-symbol daily prices need a month or trading_date",
                    source=MARKET,
                )
            params = {
                "date": f"{target.year:04d}{target.month:02d}01",
                "stockNo": symbol,
                "response": "json",
            }
            payload, meta = await self._rwd.fetch_json(
                "/afterTrading/STOCK_DAY", dataset="daily_prices", params=params
            )
            records, errors = parse_stock_day(payload, symbol)
            meta.data_as_of = records[-1]["trading_date"] if records else None
            return ProviderResult(records, meta, errors, raw_payload=payload)

        # Whole market, latest trading day only. No date parameter exists.
        payload, meta = await self._openapi.fetch_json(
            "/exchangeReport/STOCK_DAY_ALL", dataset="daily_prices"
        )
        records, errors = parse_stock_day_all(payload)
        meta.data_as_of = records[0]["trading_date"] if records else None

        if trading_date is not None and meta.data_as_of != trading_date:
            # The snapshot repeats the previous session on non-trading days.
            # Refusing here is what stops a holiday from being written as a
            # duplicate of the day before.
            raise ProviderError(
                ProviderErrorKind.NO_DATA,
                f"snapshot is for {meta.data_as_of}, not the requested {trading_date}",
                source=MARKET,
            )
        return ProviderResult(records, meta, errors, raw_payload=payload)

    async def get_market_index(self, *, trading_date: date | None = None) -> ProviderResult:
        payload, meta = await self._openapi.fetch_json(
            "/exchangeReport/MI_INDEX", dataset="index_quotes"
        )
        records, errors = parse_index_quotes(payload)
        meta.data_as_of = records[0]["trading_date"] if records else None
        if trading_date is not None and meta.data_as_of != trading_date:
            raise ProviderError(
                ProviderErrorKind.NO_DATA,
                f"snapshot is for {meta.data_as_of}, not the requested {trading_date}",
                source=MARKET,
            )
        return ProviderResult(records, meta, errors, raw_payload=payload)

    async def get_institutional_flow(self, *, trading_date: date) -> ProviderResult:
        params = {
            "date": f"{trading_date.year:04d}{trading_date.month:02d}{trading_date.day:02d}",
            "selectType": "ALL",
            "response": "json",
        }
        payload, meta = await self._rwd.fetch_json(
            "/fund/T86", dataset="institutional_flow", params=params
        )
        records, errors = parse_institutional_flow(payload, trading_date)
        meta.data_as_of = trading_date
        return ProviderResult(records, meta, errors, raw_payload=payload)

    async def health(self) -> ProviderHealth:
        ok, latency, error = await self._openapi.probe("/exchangeReport/MI_INDEX")
        return ProviderHealth(
            source=MARKET,
            reachable=ok,
            latency_ms=latency,
            checked_at=utcnow(),
            error=error,
            detail={"base_url": OPENAPI_BASE, "transport": str(self.transport)},
        )


__all__ = [
    "INDUSTRY_NAMES",
    "MARKET_INDEX_NAMES",
    "OPENAPI_BASE",
    "RWD_BASE",
    "TWSEProvider",
    "parse_holiday_schedule",
    "parse_index_quotes",
    "parse_institutional_flow",
    "parse_stock_day",
    "parse_stock_day_all",
    "parse_stock_master",
]
