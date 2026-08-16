"""Parsing contract tests.

Every case here is a quirk observed in a real response, not an invented edge
case. The fixtures they mirror are in `tests/fixtures/twse/`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

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
    to_roc_compact,
)


# ------------------------------------------------------------------ dates
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1150731", date(2026, 7, 31)),  # compact ROC, STOCK_DAY_ALL / MI_INDEX
        ("115/07/01", date(2026, 7, 1)),  # slashed ROC, STOCK_DAY rows
        ("1150101", date(2026, 1, 1)),
        ("20260731", date(2026, 7, 31)),  # some endpoints answer in Gregorian
        ("2026-07-31", date(2026, 7, 31)),
    ],
)
def test_parse_roc_date(raw, expected):
    assert parse_roc_date(raw) == expected


def test_roc_round_trip():
    assert to_roc_compact(date(2026, 7, 31)) == "1150731"
    assert parse_roc_date(to_roc_compact(date(2026, 1, 5))) == date(2026, 1, 5)


@pytest.mark.parametrize("raw", ["", "abc", "115/13/01", "1152999", None, "--"])
def test_invalid_dates_raise(raw):
    with pytest.raises(ParseError):
        parse_roc_date(raw)


def test_implausible_year_rejected():
    """A Gregorian year misread as ROC lands in the 39th century, which the
    plausibility guard catches even though the date itself is well-formed."""
    with pytest.raises(ParseError, match="implausible year"):
        parse_roc_date("2020101")  # ROC 202 -> 2113... within calendar, out of range
    with pytest.raises(ParseError, match="implausible year"):
        parse_roc_date("9990101")  # ROC 999 -> 2910


# --------------------------------------------------------------- numbers
def test_thousands_separators():
    assert parse_decimal("37,544,470") == Decimal("37544470")
    assert parse_int("1,150,086") == 1150086


def test_leading_space_zero():
    """2330 on 2026-07-27 reports 漲跌價差 as `" 0.00"`, with a leading space."""
    assert parse_decimal(" 0.00") == Decimal("0.00")
    value, comparable = parse_change(" 0.00")
    assert value == Decimal("0.00")
    assert comparable is True


@pytest.mark.parametrize("raw", ["--", "", "-", "N/A", "－", None])
def test_null_tokens(raw):
    assert parse_decimal(raw) is None


def test_null_not_allowed_raises():
    with pytest.raises(ParseError):
        parse_decimal("--", allow_null=False)


def test_x_is_not_a_number():
    """`X` means 'not comparable' (ex-dividend). Treating it as 0 would
    fabricate a flat session."""
    with pytest.raises(ParseError, match="not-comparable"):
        parse_decimal("X")

    value, comparable = parse_change("X0.00")
    assert value is None
    assert comparable is False


def test_int_rejects_fractional():
    with pytest.raises(ParseError, match="integer"):
        parse_int("1.5")


# ----------------------------------------------------------------- signs
def test_sign_in_the_number():
    """STOCK_DAY carries the sign inside 漲跌價差."""
    assert parse_change("+95.00")[0] == Decimal("95.00")
    assert parse_change("-40.00")[0] == Decimal("-40.00")


def test_sign_in_a_separate_field():
    """MI_INDEX puts the sign in 漲跌 and leaves 漲跌點數 unsigned.

    Regression: `"-"` is also a null token, so running the sign through
    `clean()` returned None and turned every decline into a rise. TAIEX was
    reported as +210.47 on a day it fell 210.47.
    """
    assert parse_change("210.47", sign="-")[0] == Decimal("-210.47")
    assert parse_change("192.89", sign="+")[0] == Decimal("192.89")
    assert parse_change("10.00", sign="")[0] == Decimal("10.00")


def test_separate_sign_field_x():
    value, comparable = parse_change("0.00", sign="X")
    assert value is None and comparable is False


# --------------------------------------------------------------- symbols
@pytest.mark.parametrize("raw", ["2330", "00981A", "00400A", " 6770 ", "2330"])
def test_valid_symbols(raw):
    assert parse_symbol(raw) == raw.strip().upper()


@pytest.mark.parametrize("raw", ["", "23", "台積電", "12345678901", None, "--"])
def test_invalid_symbols(raw):
    with pytest.raises(ParseError):
        parse_symbol(raw)


# ----------------------------------------------------------------- names
def test_trailing_padding_collapsed():
    """T86 pads security names to a fixed width: `"力積電          "`."""
    assert parse_name("力積電          ") == "力積電"
    assert parse_name("主動統一升級50  ") == "主動統一升級50"


def test_par_value_extraction():
    """t187ap03_L pads the par value mid-string."""
    assert parse_par_value("新台幣                 10.0000元") == Decimal("10.0000")
    assert parse_par_value("－ ") is None


def test_fullwidth_dash_is_null():
    assert clean("－ ") is None
    assert clean("　") is None


# ------------------------------------------------------------------ rows
def test_rows_to_dicts():
    out = rows_to_dicts(["a", "b"], [["1", "2"], ["3", "4"]])
    assert out == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_row_arity_mismatch_raises():
    """A row that does not match the header is a schema change, not a data
    error — truncating or padding would hide it."""
    with pytest.raises(ParseError, match="header has"):
        rows_to_dicts(["a", "b", "c"], [["1", "2"]])
