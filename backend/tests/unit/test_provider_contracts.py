"""Provider → canonical model contract tests.

These run the production parsers against **verbatim recorded exchange
responses**. They assert the contract the rest of the platform depends on:
required fields present, correct types, correct timezone handling, trading date
taken from the payload, symbol format, nullability, and source metadata.

If TWSE changes a field name, these fail — which is the point.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.providers import twse
from app.providers.base import ProviderError, ProviderErrorKind

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "twse"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------- calendar
class TestTradingCalendarContract:
    def test_closures_parsed(self):
        records, _errors = twse.parse_holiday_schedule(load("holiday_schedule_2026.json"), 2026)
        assert len(records) == 24
        assert all(r["is_trading_day"] is False for r in records)
        assert all(isinstance(r["calendar_date"], date) for r in records)

    def test_annotation_rows_are_not_closures(self):
        """The schedule contains rows that mark *trading* days. Treating every
        row as a holiday would delete three real sessions from 2026."""
        records, errors = twse.parse_holiday_schedule(load("holiday_schedule_2026.json"), 2026)
        closure_dates = {r["calendar_date"] for r in records}

        # 農曆春節前最後交易日 — the market trades on this day.
        assert date(2026, 2, 11) not in closure_dates
        # 國曆新年開始交易日
        assert date(2026, 1, 2) not in closure_dates
        assert len(errors) == 3
        assert all("not a closure" in e["note"] for e in errors)

    def test_settlement_only_days_distinguished(self):
        records, _ = twse.parse_holiday_schedule(load("holiday_schedule_2026.json"), 2026)
        settlement = [r for r in records if r["session_type"] == "SETTLEMENT_ONLY"]
        assert {r["calendar_date"] for r in settlement} == {date(2026, 2, 12), date(2026, 2, 13)}

    def test_schema_change_raises(self):
        with pytest.raises(ProviderError) as exc:
            twse.parse_holiday_schedule({"unexpected": "object"}, 2026)
        assert exc.value.kind is ProviderErrorKind.SCHEMA_CHANGED


# --------------------------------------------------------------- prices
class TestDailyPriceContract:
    def test_required_fields_and_types(self):
        records, errors = twse.parse_stock_day(load("stock_day_2330_202607.json"), "2330")
        assert not errors
        assert len(records) == 22

        for r in records:
            assert r["symbol"] == "2330"
            assert r["market"] == "TWSE"
            assert isinstance(r["trading_date"], date)
            assert r["source"] == "TWSE"
            for f in ("open", "high", "low", "close"):
                assert isinstance(r[f], Decimal), f"{f} must be Decimal, not float"
            assert isinstance(r["volume"], int)

    def test_trading_date_comes_from_the_payload(self):
        """Not from the clock. This is the single most important contract in
        the ingestion path."""
        records, _ = twse.parse_stock_day(load("stock_day_2330_202607.json"), "2330")
        dates = {r["trading_date"] for r in records}
        assert min(dates) == date(2026, 7, 1)
        assert max(dates) == date(2026, 7, 31)
        # July 2026 weekends are absent because the exchange did not report them.
        assert date(2026, 7, 4) not in dates
        assert date(2026, 7, 5) not in dates

    def test_ohlc_coherence_in_real_data(self):
        records, _ = twse.parse_stock_day(load("stock_day_2330_202607.json"), "2330")
        for r in records:
            assert r["low"] <= r["open"] <= r["high"]
            assert r["low"] <= r["close"] <= r["high"]

    def test_zero_change_with_leading_space(self):
        records, _ = twse.parse_stock_day(load("stock_day_2330_202607.json"), "2330")
        row = next(r for r in records if r["trading_date"] == date(2026, 7, 27))
        assert row["change"] == Decimal("0.00")

    def test_known_values_exact(self):
        """Guards against a silent unit or scaling change."""
        records, _ = twse.parse_stock_day(load("stock_day_2330_202607.json"), "2330")
        row = next(r for r in records if r["trading_date"] == date(2026, 7, 17))
        assert row["open"] == Decimal("2375.00")
        assert row["high"] == Decimal("2395.00")
        assert row["low"] == Decimal("2290.00")
        assert row["close"] == Decimal("2290.00")
        assert row["volume"] == 97_362_670
        assert row["turnover"] == Decimal("229051751965")
        assert row["trade_count"] == 1_150_086
        assert row["change"] == Decimal("-180.00")

    def test_snapshot_contract(self):
        records, errors = twse.parse_stock_day_all(load("stock_day_all_1150814.json"))
        assert not errors
        assert len(records) == 8
        assert all(r["trading_date"] == date(2026, 8, 14) for r in records)

    def test_alphanumeric_etf_symbols_accepted(self):
        """Active ETFs have letters in their codes. A digits-only rule would
        silently drop every one of them."""
        records, _ = twse.parse_stock_day_all(load("stock_day_all_1150814.json"))
        symbols = {r["symbol"] for r in records}
        assert "00400A" in symbols
        assert all(s.isalnum() for s in symbols)

    def test_no_data_raises_rather_than_returning_empty(self):
        with pytest.raises(ProviderError) as exc:
            twse.parse_stock_day({"stat": "很抱歉，沒有符合條件的資料!"}, "9999")
        assert exc.value.kind is ProviderErrorKind.NO_DATA


# -------------------------------------------------------------- indices
class TestIndexContract:
    def test_required_fields(self):
        records, errors = twse.parse_index_quotes(load("mi_index_1150814.json"))
        assert not errors
        assert len(records) == 12
        for r in records:
            assert r["index_code"] and r["index_name"]
            assert isinstance(r["trading_date"], date)
            assert isinstance(r["close"], Decimal)
            assert r["source"] == "TWSE"

    def test_headline_index_mapped_to_stable_code(self):
        records, _ = twse.parse_index_quotes(load("mi_index_1150814.json"))
        taiex = next(r for r in records if r["index_code"] == "TAIEX")
        assert taiex["index_name"] == "發行量加權股價指數"
        assert taiex["index_type"] == "MARKET"

    def test_sign_taken_from_the_separate_field(self):
        records, _ = twse.parse_index_quotes(load("mi_index_1150814.json"))
        taiex = next(r for r in records if r["index_code"] == "TAIEX")
        assert taiex["close"] == Decimal("45811.01")
        assert taiex["change"] == Decimal("-210.47"), "漲跌='-' must make this negative"
        assert taiex["change_pct"] == Decimal("-0.46")

        rising = next(r for r in records if r["index_name"] == "臺灣發達指數")
        assert rising["change"] == Decimal("192.89")


# -------------------------------------------------------- institutional
class TestInstitutionalFlowContract:
    def test_one_record_per_investor_type(self):
        records, errors = twse.parse_institutional_flow(
            load("t86_20260814.json"), date(2026, 8, 14)
        )
        assert not errors
        by_symbol: dict[str, set[str]] = {}
        for r in records:
            by_symbol.setdefault(r["symbol"], set()).add(r["investor_type"])
        assert by_symbol["6770"] == {
            "FOREIGN",
            "FOREIGN_DEALER",
            "INVESTMENT_TRUST",
            "DEALER_SELF",
            "DEALER_HEDGE",
            "DEALER",
            "TOTAL",
        }

    def test_buy_and_sell_preserved_not_just_net(self):
        """Net alone discards turnover: 1M bought against 1M sold and no
        activity both net to zero."""
        records, _ = twse.parse_institutional_flow(load("t86_20260814.json"), date(2026, 8, 14))
        foreign = next(
            r for r in records if r["symbol"] == "6770" and r["investor_type"] == "FOREIGN"
        )
        assert foreign["buy_volume"] == 253_882_636
        assert foreign["sell_volume"] == 108_916_172
        assert foreign["net_volume"] == 144_966_464
        assert foreign["buy_volume"] - foreign["sell_volume"] == foreign["net_volume"]

    def test_negative_net_preserved(self):
        records, _ = twse.parse_institutional_flow(load("t86_20260814.json"), date(2026, 8, 14))
        trust = next(
            r for r in records if r["symbol"] == "6770" and r["investor_type"] == "INVESTMENT_TRUST"
        )
        assert trust["net_volume"] == -27_000

    def test_padded_names_do_not_break_symbol_parsing(self):
        records, errors = twse.parse_institutional_flow(
            load("t86_20260814.json"), date(2026, 8, 14)
        )
        assert not errors
        assert "00403A" in {r["symbol"] for r in records}


# --------------------------------------------------------- stock master
class TestStockMasterContract:
    def test_required_fields_and_industry_mapping(self):
        records, errors = twse.parse_stock_master(load("t187ap03_L.json"))
        assert not errors
        tcc = next(r for r in records if r["symbol"] == "1101")
        assert tcc["name"] == "臺灣水泥股份有限公司"
        assert tcc["short_name"] == "台泥"
        assert tcc["short_name_en"] == "TCC"
        assert tcc["industry_code"] == "01"
        assert tcc["industry_name"] == "水泥工業"
        assert tcc["listing_date"] == date(1962, 2, 9)
        assert tcc["par_value"] == Decimal("10.0000")
        assert tcc["shares_outstanding"] == 7_523_181_742

    def test_listing_date_is_gregorian_in_this_endpoint(self):
        """`上市日期` is `19620209`, not ROC — mixing calendars within one
        source is exactly the sort of thing that needs a test."""
        records, _ = twse.parse_stock_master(load("t187ap03_L.json"))
        assert all(
            r["listing_date"] is None or 1900 < r["listing_date"].year < 2100 for r in records
        )
