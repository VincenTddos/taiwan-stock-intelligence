"""Parsing primitives for Taiwanese exchange payloads.

Every function here exists because a real response needed it. The quirks are not
hypothetical — each is reproduced in `tests/fixtures/twse/`:

* ROC calendar dates in two shapes: `1150731` and `115/07/01`
* Every value is a string, including numbers
* Thousands separators: `"37,544,470"`
* A **leading space** before a zero change: `" 0.00"` (2330 on 2026-07-27)
* Names padded with trailing spaces: `"力積電          "` (T86)
* Par value padded mid-string: `"新台幣                 10.0000元"`
* Full-width dash as null: `"－ "`
* Sign carried in a separate field (`漲跌` = `+` / `-` / `X`), not in the number
* `X` meaning "not comparable" (ex-dividend), which is **not** zero

The functions are deliberately strict: they raise `ParseError` rather than
returning a plausible-looking wrong value. A parse failure sends the record to
quarantine, where a human can see it. A silent coercion becomes a wrong price.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Values that mean "no data" across TWSE/TPEx payloads.
NULL_TOKENS: frozenset[str] = frozenset(
    {"", "-", "--", "---", "N/A", "n/a", "null", "None", "－", "—", "‑", "0.00元"}
)

_ROC_COMPACT = re.compile(r"^(\d{3})(\d{2})(\d{2})$")
_ROC_SLASHED = re.compile(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$")
_AD_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_AD_DASHED = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")

ROC_OFFSET = 1911


class ParseError(ValueError):
    """Raised when a value cannot be parsed with confidence.

    Carries the original text so the quarantine record shows exactly what the
    source sent.
    """

    def __init__(self, field: str, raw: object, reason: str) -> None:
        self.field = field
        self.raw = raw
        self.reason = reason
        super().__init__(f"{field}: {reason} (raw={raw!r})")


def clean(value: object) -> str | None:
    """Strip whitespace (including full-width) and map null tokens to None."""
    if value is None:
        return None
    text = str(value).replace("　", " ").strip()
    if text in NULL_TOKENS:
        return None
    return text or None


def parse_roc_date(value: object, *, field: str = "date") -> date:
    """Parse a Taiwanese ROC-calendar date.

    Accepts `1150731`, `115/07/01`, and — because some endpoints mix calendars —
    Gregorian `20260731` and `2026-07-31`.
    """
    text = clean(value)
    if text is None:
        raise ParseError(field, value, "empty date")
    text = text.replace(" ", "")

    if (m := _ROC_COMPACT.match(text)) or (m := _ROC_SLASHED.match(text)):
        y, mo, d = int(m[1]) + ROC_OFFSET, int(m[2]), int(m[3])
    elif (m := _AD_COMPACT.match(text)) or (m := _AD_DASHED.match(text)):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        raise ParseError(field, value, "unrecognised date format")

    try:
        parsed = date(y, mo, d)
    except ValueError as exc:
        raise ParseError(field, value, f"invalid calendar date: {exc}") from exc

    # A ROC year parsed as Gregorian would land in the 2nd century; a Gregorian
    # year parsed as ROC would land in the 40th. Both are obvious mistakes.
    if not (1960 <= parsed.year <= 2100):
        raise ParseError(field, value, f"implausible year {parsed.year}")
    return parsed


def to_roc_compact(value: date) -> str:
    """Inverse of `parse_roc_date` for the compact form, used to build requests."""
    return f"{value.year - ROC_OFFSET:03d}{value.month:02d}{value.day:02d}"


def parse_decimal(
    value: object, *, field: str = "value", allow_null: bool = True
) -> Decimal | None:
    """Parse a numeric string that may carry separators, padding or a sign.

    `" 0.00"` (leading space) and `"+95.00"` both parse. `"X"` does not — it means
    "not comparable", which is information, not a number; callers handle it
    explicitly via `parse_change`.
    """
    text = clean(value)
    if text is None:
        if allow_null:
            return None
        raise ParseError(field, value, "empty numeric")

    if text.upper() == "X":
        raise ParseError(field, value, "'X' means not-comparable, not a number")

    normalized = text.replace(",", "").replace("+", "").replace("%", "").replace("元", "")
    normalized = normalized.replace(" ", "")
    if normalized in ("", "-", "."):
        if allow_null:
            return None
        raise ParseError(field, value, "no digits")

    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise ParseError(field, value, "not a decimal") from exc


def parse_int(value: object, *, field: str = "value", allow_null: bool = True) -> int | None:
    dec = parse_decimal(value, field=field, allow_null=allow_null)
    if dec is None:
        return None
    if dec != dec.to_integral_value():
        raise ParseError(field, value, "expected an integer")
    return int(dec)


def parse_change(
    value: object, *, sign: object = None, field: str = "change"
) -> tuple[Decimal | None, bool]:
    """Parse a price change, returning `(value, is_comparable)`.

    Two payload shapes exist:

    * STOCK_DAY puts the sign in the number: `"+95.00"`, `"-40.00"`, `" 0.00"`,
      and `"X0.00"` when the day is not comparable to the previous close.
    * MI_INDEX puts the sign in a separate `漲跌` field with the magnitude
      unsigned.

    `is_comparable=False` means the exchange is telling us a comparison is
    meaningless (typically ex-dividend). Storing `0` there would fabricate a
    flat day — the caller must store NULL instead.
    """
    text = clean(value)
    if text is None:
        return None, True

    # NOT `clean()`: a bare "-" is a legitimate *sign* here, but it is also a
    # null token elsewhere. Running it through clean() would return None and
    # silently turn every decline into a rise — which is exactly what happened
    # to TAIEX before this was fixed.
    sign_text = str(sign).strip() if sign is not None else ""

    if sign_text.upper() == "X" or text.upper().startswith("X"):
        return None, False

    magnitude = parse_decimal(text.lstrip("Xx"), field=field)
    if magnitude is None:
        return None, True

    if sign_text == "-":
        magnitude = -abs(magnitude)
    elif sign_text == "+":
        magnitude = abs(magnitude)
    return magnitude, True


def parse_symbol(value: object, *, field: str = "symbol") -> str:
    """Normalise and validate an exchange ticker.

    Uppercased and stripped. Active ETFs legitimately contain letters
    (`00981A`), so a digits-only rule would reject real securities.
    """
    text = clean(value)
    if text is None:
        raise ParseError(field, value, "empty symbol")
    text = text.upper().replace(" ", "")
    if not re.fullmatch(r"[0-9A-Z]{4,10}", text):
        raise ParseError(field, value, "symbol must be 4-10 chars of [0-9A-Z]")
    return text


def parse_name(value: object) -> str | None:
    """Collapse the internal padding TWSE uses to align fixed-width columns."""
    text = clean(value)
    if text is None:
        return None
    return re.sub(r"\s+", " ", text).strip()


def parse_par_value(value: object) -> Decimal | None:
    """Extract the number from `"新台幣                 10.0000元"`."""
    text = clean(value)
    if text is None:
        return None
    if m := re.search(r"(\d+(?:\.\d+)?)", text.replace(",", "")):
        return Decimal(m.group(1))
    return None


def rows_to_dicts(fields: list[str], rows: list[list[object]]) -> list[dict[str, object]]:
    """Zip the `fields`/`data` shape used by the TWSE RWD endpoints.

    Rows whose arity does not match the header are a schema change, not a data
    error, so they raise rather than being silently truncated or padded.
    """
    out: list[dict[str, object]] = []
    for idx, row in enumerate(rows):
        if len(row) != len(fields):
            raise ParseError(
                "row", row, f"row {idx} has {len(row)} values, header has {len(fields)}"
            )
        out.append(dict(zip(fields, row, strict=True)))
    return out


def utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


__all__ = [
    "NULL_TOKENS",
    "ROC_OFFSET",
    "ParseError",
    "clean",
    "parse_change",
    "parse_decimal",
    "parse_int",
    "parse_name",
    "parse_par_value",
    "parse_roc_date",
    "parse_symbol",
    "rows_to_dicts",
    "to_roc_compact",
    "utcnow",
]
