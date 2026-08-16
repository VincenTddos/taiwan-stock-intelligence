"""Data validation and quality.

Three severities, three outcomes:

* **FATAL** — the record cannot be stored coherently (OHLC that contradicts
  itself, a trading date that is not a trading day, a malformed symbol). It goes
  to quarantine with the raw payload attached. It is *not* discarded: a source
  changing its format must show up as a spike in quarantine, not as a quiet day
  with less data.
* **WARN** — the record is storable but suspicious (a 40% single-day move, zero
  volume with a price change). It is stored with `quality_status='SUSPECT'` and
  the rule id recorded. Deleting anomalous prices would delete real limit-up
  days, real crashes, and real news reactions — the very events the platform
  exists to study.
* **INFO** — noted in metrics only.

The distinction that matters: validity is about *internal coherence*, which we
can judge; anomaly is about *plausibility*, which we cannot judge without
context. Only the first is allowed to reject.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)


class Severity(StrEnum):
    FATAL = "FATAL"
    WARN = "WARN"
    INFO = "INFO"


@dataclass(slots=True)
class Violation:
    rule_id: str
    severity: Severity
    message: str
    field: str | None = None
    value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": str(self.severity),
            "message": self.message,
            "field": self.field,
            "value": str(self.value) if self.value is not None else None,
        }


@dataclass(slots=True)
class ValidationResult:
    record: dict[str, Any]
    violations: list[Violation] = field(default_factory=list)

    @property
    def fatal(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.FATAL]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.WARN]

    @property
    def accepted(self) -> bool:
        return not self.fatal

    @property
    def quality_status(self) -> str:
        if self.fatal:
            return "INVALID"
        return "SUSPECT" if self.warnings else "OK"

    @property
    def flags(self) -> list[str]:
        return [v.rule_id for v in self.violations]


# Taiwan's daily limit is ±10%. The threshold is set above it because limit-up
# on a resumed-from-suspension stock, and stocks with no limit, both exceed 10%
# legitimately. Anything past 50% in one session is almost certainly bad data.
LIMIT_PCT = Decimal("0.10")
SUSPECT_MOVE_PCT = Decimal("0.105")
IMPLAUSIBLE_MOVE_PCT = Decimal("0.50")


class DataQualityService:
    """Stateless validators. Each returns violations; the caller decides."""

    # ---------------------------------------------------------- validity
    def validate_daily_price(
        self, rec: dict[str, Any], *, prev_close: Decimal | None = None
    ) -> ValidationResult:
        v: list[Violation] = []
        o, h, low, c = rec.get("open"), rec.get("high"), rec.get("low"), rec.get("close")
        volume, turnover = rec.get("volume"), rec.get("turnover")

        if not rec.get("symbol"):
            v.append(Violation("P00", Severity.FATAL, "missing symbol", "symbol"))
        if not rec.get("trading_date"):
            v.append(Violation("P01", Severity.FATAL, "missing trading_date", "trading_date"))
        if not rec.get("source"):
            v.append(Violation("P02", Severity.FATAL, "missing source", "source"))

        # --- OHLC coherence: the definition of high and low -------------
        if h is not None and low is not None and h < low:
            v.append(Violation("P10", Severity.FATAL, f"high {h} < low {low}", "high", h))
        for name, val in (("open", o), ("close", c)):
            if val is None:
                continue
            if h is not None and val > h:
                v.append(Violation("P11", Severity.FATAL, f"{name} {val} > high {h}", name, val))
            if low is not None and val < low:
                v.append(Violation("P12", Severity.FATAL, f"{name} {val} < low {low}", name, val))

        for name in ("open", "high", "low", "close"):
            val = rec.get(name)
            if val is not None and val < 0:
                v.append(Violation("P13", Severity.FATAL, f"{name} is negative", name, val))
            elif val is not None and val == 0 and volume:
                v.append(
                    Violation(
                        "P14",
                        Severity.WARN,
                        f"{name} is zero on a day with volume {volume}",
                        name,
                        val,
                    )
                )

        if volume is not None and volume < 0:
            v.append(Violation("P15", Severity.FATAL, "negative volume", "volume", volume))
        if turnover is not None and turnover < 0:
            v.append(Violation("P16", Severity.FATAL, "negative turnover", "turnover", turnover))

        # --- plausibility: recorded, never rejected ---------------------
        if volume == 0 and c is not None and o is not None and c != o:
            v.append(
                Violation("P20", Severity.WARN, "price moved on zero volume", "volume", volume)
            )
        if volume and turnover is not None and turnover == 0:
            v.append(Violation("P21", Severity.WARN, "volume without turnover", "turnover"))

        if prev_close is not None and prev_close > 0 and c is not None:
            move = abs(c - prev_close) / prev_close
            if move > IMPLAUSIBLE_MOVE_PCT:
                v.append(
                    Violation(
                        "P30",
                        Severity.WARN,
                        f"{move:.1%} move vs previous close {prev_close} — "
                        f"verify corporate action or bad data",
                        "close",
                        c,
                    )
                )
            elif move > SUSPECT_MOVE_PCT:
                v.append(
                    Violation(
                        "P31",
                        Severity.WARN,
                        f"{move:.1%} move exceeds the ±10% daily limit",
                        "close",
                        c,
                    )
                )

        # Turnover should be roughly volume x average price. A large mismatch
        # usually means one of the two was parsed in the wrong unit.
        if volume and turnover and h and low and volume > 0:
            implied = turnover / volume
            if implied > h * Decimal("1.5") or implied < low * Decimal("0.5"):
                v.append(
                    Violation(
                        "P32",
                        Severity.WARN,
                        f"implied average price {implied:.2f} outside the day's "
                        f"range [{low}, {h}] — possible unit mismatch",
                        "turnover",
                        turnover,
                    )
                )

        return ValidationResult(rec, v)

    def validate_index_quote(self, rec: dict[str, Any]) -> ValidationResult:
        v: list[Violation] = []
        if not rec.get("index_code"):
            v.append(Violation("I00", Severity.FATAL, "missing index_code", "index_code"))
        if not rec.get("trading_date"):
            v.append(Violation("I01", Severity.FATAL, "missing trading_date", "trading_date"))
        close = rec.get("close")
        if close is None:
            v.append(Violation("I02", Severity.FATAL, "missing close", "close"))
        elif close <= 0:
            v.append(Violation("I03", Severity.FATAL, "close is not positive", "close", close))

        h, low = rec.get("high"), rec.get("low")
        if h is not None and low is not None and h < low:
            v.append(Violation("I10", Severity.FATAL, f"high {h} < low {low}", "high", h))
        return ValidationResult(rec, v)

    def validate_institutional_flow(self, rec: dict[str, Any]) -> ValidationResult:
        v: list[Violation] = []
        if not rec.get("symbol"):
            v.append(Violation("F00", Severity.FATAL, "missing symbol", "symbol"))
        if not rec.get("trading_date"):
            v.append(Violation("F01", Severity.FATAL, "missing trading_date", "trading_date"))
        if not rec.get("investor_type"):
            v.append(Violation("F02", Severity.FATAL, "missing investor_type", "investor_type"))

        buy, sell, net = rec.get("buy_volume"), rec.get("sell_volume"), rec.get("net_volume")
        for name, val in (("buy_volume", buy), ("sell_volume", sell)):
            if val is not None and val < 0:
                v.append(Violation("F10", Severity.FATAL, f"negative {name}", name, val))

        # Net must equal buy - sell when all three are present. A mismatch means
        # a column was mapped wrongly, which is a schema problem, not a data one.
        if buy is not None and sell is not None and net is not None and buy - sell != net:
            v.append(
                Violation(
                    "F11",
                    Severity.FATAL,
                    f"net {net} != buy {buy} - sell {sell}",
                    "net_volume",
                    net,
                )
            )
        return ValidationResult(rec, v)

    def validate_stock_master(self, rec: dict[str, Any]) -> ValidationResult:
        v: list[Violation] = []
        if not rec.get("symbol"):
            v.append(Violation("M00", Severity.FATAL, "missing symbol", "symbol"))
        if not rec.get("name"):
            v.append(Violation("M01", Severity.FATAL, "missing name", "name"))

        listing, delisting = rec.get("listing_date"), rec.get("delisting_date")
        if listing and delisting and delisting < listing:
            v.append(
                Violation("M10", Severity.FATAL, "delisting precedes listing", "delisting_date")
            )
        if listing and isinstance(listing, date) and listing.year < 1960:
            v.append(
                Violation(
                    "M11",
                    Severity.WARN,
                    f"listing_date {listing} looks wrong",
                    "listing_date",
                    listing,
                )
            )
        return ValidationResult(rec, v)

    # -------------------------------------------------------- completeness
    def check_completeness(
        self,
        *,
        expected_dates: list[date],
        observed_dates: set[date],
        expected_symbols: set[str] | None = None,
        observed_symbols: set[str] | None = None,
    ) -> dict[str, Any]:
        """Which trading days and symbols are missing.

        `expected_dates` comes from the trading calendar, never from a date
        range: a naive range would count every weekend as a gap.
        """
        missing_dates = sorted(d for d in expected_dates if d not in observed_dates)
        result: dict[str, Any] = {
            "expected_days": len(expected_dates),
            "observed_days": len(observed_dates & set(expected_dates)),
            "missing_days": [d.isoformat() for d in missing_dates],
            "completeness_pct": (
                round(100 * (1 - len(missing_dates) / len(expected_dates)), 2)
                if expected_dates
                else None
            ),
        }
        if expected_symbols is not None and observed_symbols is not None:
            missing_symbols = sorted(expected_symbols - observed_symbols)
            result["missing_symbols"] = missing_symbols[:100]
            result["missing_symbol_count"] = len(missing_symbols)
        return result

    # ---------------------------------------------------------- continuity
    def check_continuity(self, series: list[dict[str, Any]]) -> list[Violation]:
        """Cross-row checks on a single symbol's chronological series."""
        v: list[Violation] = []
        seen: set[date] = set()
        prev: dict[str, Any] | None = None

        for rec in series:
            day = rec.get("trading_date")
            if day in seen:
                v.append(Violation("C00", Severity.FATAL, f"duplicate date {day}", "trading_date"))
            seen.add(day)  # type: ignore[arg-type]

            if prev is not None:
                pc, cc = prev.get("close"), rec.get("close")
                if pc and cc and pc > 0:
                    move = abs(cc - pc) / pc
                    if move > IMPLAUSIBLE_MOVE_PCT:
                        v.append(
                            Violation(
                                "C10",
                                Severity.WARN,
                                f"{move:.1%} jump from {prev.get('trading_date')} to {day}",
                                "close",
                                cc,
                            )
                        )
                pv, cv = prev.get("volume"), rec.get("volume")
                if pv and cv and pv > 0 and cv / pv > 20:
                    v.append(
                        Violation(
                            "C11",
                            Severity.WARN,
                            f"volume {cv / pv:.0f}x the previous session",
                            "volume",
                            cv,
                        )
                    )
            prev = rec
        return v


__all__ = [
    "IMPLAUSIBLE_MOVE_PCT",
    "LIMIT_PCT",
    "SUSPECT_MOVE_PCT",
    "DataQualityService",
    "Severity",
    "ValidationResult",
    "Violation",
]
