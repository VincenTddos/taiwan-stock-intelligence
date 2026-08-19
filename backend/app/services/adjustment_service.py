"""Adjustment factors, and the adjusted price series derived from them.

`corporate_action_service` answers whether a series *may* be adjusted. This
module answers what the adjustment *is*. They are separate because the guard has
to be able to refuse without knowing any arithmetic, and the arithmetic has to
be readable without the guard tangled through it.

Three decisions live here rather than in a provider, because all three are
market-specific and more than one is defensible:

**The formula is the exchange's own.** TWSE publishes a reference price for
every ex-rights and ex-dividend event:

    reference = (close − cash + subscription_ratio × subscription_price)
                / (1 + stock_ratio + subscription_ratio)

and the day's factor is `reference / close`. Adopting a different convention
would put this platform in permanent small disagreement with every published
Taiwanese number, which is worse than being wrong in a visible way.

**Components multiply out exactly.** One ex-date may carry a cash dividend, a
stock dividend and a rights issue at once, and each is stored as its own row.
Each row therefore holds the part of the day's factor attributable to it, split
so that the product over the day's rows is the day factor to the last digit:

    cash    (P − D) / P
    rights  (P − D + kS) / ((P − D)(1 + k))
    stock   (1 + k) / (1 + s + k)

Multiply them and the (P − D) and (1 + k) terms cancel, leaving
(P − D + kS) / (P(1 + s + k)) — the exchange's formula. With one component
present the other two are 1 and each reduces to its textbook form, which is what
happens in the overwhelming majority of events.

**A factor is checked, not asserted.** Where the exchange published its own
before and after prices, `reference_price_after / reference_price_before` is an
independent statement of the same quantity. It is never used *as* the factor for
a decomposable event — it is compared against one, and the gap is reported in
basis points. Capital reductions are the single exception, deliberately: their
economics are two unknowns behind one observable, so the published price effect
is the only honest source and is used directly.

What this module will not do is guess. An event it cannot resolve — a rights
issue whose subscription price the company has not published, a reduction with
no reference prices, an ex-date with no prior close — is reported as unresolved
and blocks its symbol. A factor of 1.0 standing in for an event that certainly
moved the price is indistinguishable, downstream, from a day on which nothing
happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

from app.core.logging import get_logger
from app.models.market import CorporateAction, CorporateActionType, DailyPrice
from app.services.corporate_action_service import CorporateActionCoverageService

log = get_logger(__name__)

ONE = Decimal(1)
ZERO = Decimal(0)
ONE_DAY = timedelta(days=1)

#: How far a computed factor may sit from the exchange's own published one
#: before it is worth a human's attention. TWSE rounds its reference price to
#: two decimals, so a sub-NT$20 stock can differ by ~25 bps on rounding alone;
#: the threshold sits above that and far below any real error.
RECONCILIATION_TOLERANCE_BPS = Decimal("30")

#: Factors outside this band are refused outright. A tenfold price change from a
#: single corporate action is legal — a 1:10 reverse split — but not from the
#: events these sources report, so a factor out here is far more likely to be a
#: parse error that would otherwise quietly rewrite a decade of prices.
MIN_FACTOR = Decimal("0.05")
MAX_FACTOR = Decimal("20")

#: Kinds that dilute the share count. A split of n is the same shape as a stock
#: dividend of (n − 1) free shares per share, so they share one factor.
DILUTIVE = frozenset(
    {
        CorporateActionType.STOCK_DIVIDEND,
        CorporateActionType.SPLIT,
        CorporateActionType.REVERSE_SPLIT,
    }
)


@dataclass(slots=True)
class DayFactor:
    """One symbol's total price adjustment on one ex-date."""

    symbol: str
    ex_date: date
    factor: Decimal = ONE
    base_price: Decimal | None = None
    base_price_source: str = "NONE"
    components: dict[int, Decimal] = field(default_factory=dict)
    published: Decimal | None = None
    residual_bps: Decimal | None = None
    unresolved: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return not self.unresolved

    @property
    def reconciled(self) -> bool | None:
        """Agreement with the exchange's own number; None if it published none."""
        if self.residual_bps is None:
            return None
        return self.residual_bps <= RECONCILIATION_TOLERANCE_BPS

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "ex_date": self.ex_date.isoformat(),
            "factor": str(round(self.factor, 10)),
            "base_price": str(self.base_price) if self.base_price is not None else None,
            "base_price_source": self.base_price_source,
            "published_factor": (
                str(round(self.published, 10)) if self.published is not None else None
            ),
            "residual_bps": (
                str(round(self.residual_bps, 2)) if self.residual_bps is not None else None
            ),
            "reconciled": self.reconciled,
            "unresolved": self.unresolved,
        }


@dataclass(slots=True)
class AdjustmentReport:
    """What one `apply` did, and what it refused to do.

    A dataclass rather than a dict because the two lists that matter —
    `unresolved` and `mismatched` — are the ones a caller is most likely to skip
    reading. Named fields make ignoring them a visible choice.
    """

    symbols: int
    adjusted: int
    blocked: list[str]
    events: int
    unresolved: list[DayFactor]
    mismatched: list[DayFactor]
    rows_written: int

    def as_dict(self) -> dict[str, object]:
        return {
            "symbols": self.symbols,
            "adjusted": self.adjusted,
            "blocked": self.blocked,
            "events": self.events,
            "unresolved": [f.as_dict() for f in self.unresolved],
            "mismatched": [f.as_dict() for f in self.mismatched],
            "rows_written": self.rows_written,
        }


def _dec(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) else None


def _positive_ref(actions: list[CorporateAction], attr: str) -> Decimal | None:
    for a in actions:
        value = _dec(getattr(a, attr))
        if value is not None and value > 0:
            return value
    return None


class AdjustmentService:
    """Computes day factors, and writes the back-adjusted series they imply."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.coverage = CorporateActionCoverageService(session)

    # ------------------------------------------------------------- reading
    async def _actions(
        self,
        symbols: list[str],
        after: date,
        market: str,
        as_of: datetime | None,
    ) -> dict[tuple[str, date], list[CorporateAction]]:
        stmt = select(CorporateAction).where(
            CorporateAction.symbol.in_(symbols),
            CorporateAction.market == market,
            # Strictly after: a bar on the ex-date already reflects the event.
            CorporateAction.ex_date > after,
        )
        if as_of is not None:
            # Point in time. An adjustment applied to a backtest that has not
            # reached the announcement is the future leaking into the past
            # through a column nobody thinks of as data.
            stmt = stmt.where(CorporateAction.announced_at <= as_of)

        grouped: dict[tuple[str, date], list[CorporateAction]] = {}
        for row in (await self.session.execute(stmt.order_by(CorporateAction.id))).scalars():
            grouped.setdefault((row.symbol, row.ex_date), []).append(row)
        return grouped

    async def _prior_closes(
        self, wanted: set[tuple[str, date]], market: str
    ) -> dict[tuple[str, date], Decimal]:
        """The last raw close strictly before each ex-date."""
        out: dict[tuple[str, date], Decimal] = {}
        for symbol, ex_date in wanted:
            close = (
                await self.session.execute(
                    select(DailyPrice.close)
                    .where(
                        DailyPrice.symbol == symbol,
                        DailyPrice.market == market,
                        DailyPrice.trading_date < ex_date,
                        DailyPrice.close.is_not(None),
                    )
                    .order_by(DailyPrice.trading_date.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if close is not None:
                out[(symbol, ex_date)] = close
        return out

    # ----------------------------------------------------------- the maths
    def compute_day_factor(
        self, actions: list[CorporateAction], *, prior_close: Decimal | None = None
    ) -> DayFactor:
        """One ex-date's factor, and each action's share of it.

        `prior_close` is the fallback base price. The exchange's own
        `reference_price_before` is preferred where present: it is the number the
        exchange itself put into the formula, and using it removes a whole class
        of disagreement that is really about which close was used rather than
        about the adjustment.
        """
        day = DayFactor(symbol=actions[0].symbol, ex_date=actions[0].ex_date)

        published_before = _positive_ref(actions, "reference_price_before")
        base = published_before if published_before is not None else prior_close
        day.base_price = base
        day.base_price_source = (
            "EXCHANGE_REFERENCE"
            if published_before is not None
            else ("PRIOR_CLOSE" if base is not None else "NONE")
        )

        cash = ZERO
        stock = ZERO
        sub_ratio = ZERO
        sub_price = ZERO
        by_kind: dict[CorporateActionType, list[CorporateAction]] = {}

        for a in actions:
            kind = CorporateActionType(a.action_type)
            by_kind.setdefault(kind, []).append(a)

            if kind is CorporateActionType.CASH_DIVIDEND:
                cash += _dec(a.cash_dividend) or ZERO
            elif kind is CorporateActionType.STOCK_DIVIDEND:
                stock += _dec(a.stock_dividend) or ZERO
            elif kind in (CorporateActionType.SPLIT, CorporateActionType.REVERSE_SPLIT):
                ratio = _dec(a.split_ratio)
                if ratio is None or ratio <= 0:
                    day.unresolved.append(f"{kind} on {day.ex_date} has no split ratio")
                else:
                    stock += ratio - ONE
            elif kind is CorporateActionType.RIGHTS_ISSUE:
                ratio, price = _dec(a.subscription_ratio), _dec(a.subscription_price)
                if ratio is None or price is None:
                    missing = "ratio" if ratio is None else "price"
                    day.unresolved.append(
                        f"rights issue on {day.ex_date} has no subscription {missing} yet"
                    )
                else:
                    sub_ratio += ratio
                    sub_price = price
            # CAPITAL_REDUCTION is handled below, from published prices only.
            # PAR_VALUE_CHANGE changes the stated par value, not what a holder
            # owns, and so contributes nothing — the same reason it is left out
            # of PRICE_AFFECTING.

        needs_base = cash > 0 or sub_ratio > 0
        if needs_base and (base is None or base <= 0):
            day.unresolved.append(
                f"no usable pre-event price for {day.symbol} on {day.ex_date}; "
                "a cash or rights adjustment cannot be computed without one"
            )

        cash_factor = ONE
        rights_factor = ONE
        dilution_factor = ONE
        reduction_factor = ONE

        if not day.unresolved:
            p = base if base is not None else ONE
            after_cash = p - cash
            if cash > 0:
                if after_cash <= 0:
                    day.unresolved.append(
                        f"cash of {cash} is not less than the pre-event price {p}"
                    )
                else:
                    cash_factor = after_cash / p
            if sub_ratio > 0 and after_cash > 0:
                rights_factor = (after_cash + sub_ratio * sub_price) / (
                    after_cash * (ONE + sub_ratio)
                )
            if stock > 0 or sub_ratio > 0:
                dilution_factor = (ONE + sub_ratio) / (ONE + stock + sub_ratio)

            for a in by_kind.get(CorporateActionType.CAPITAL_REDUCTION, []):
                before = _dec(a.reference_price_before)
                after = _dec(a.reference_price_after)
                if before is None or after is None or before <= 0 or after <= 0:
                    # Its ratio and its returned cash are two unknowns behind one
                    # observable. Without the exchange's published prices there
                    # is nothing here to compute from, and back-solving would
                    # produce a number with no source.
                    day.unresolved.append(
                        f"capital reduction on {day.ex_date} has no published "
                        "reference prices, and its terms cannot be recovered"
                    )
                else:
                    reduction_factor *= after / before

        if day.unresolved:
            return day

        day.factor = cash_factor * rights_factor * dilution_factor * reduction_factor
        if not (MIN_FACTOR <= day.factor <= MAX_FACTOR):
            day.unresolved.append(
                f"implied factor {day.factor:.6f} is outside [{MIN_FACTOR}, {MAX_FACTOR}]"
            )
            return day

        # Attribute the day's factor across its rows, so the product over the
        # rows is exactly the day factor. Each factor is carried by one row; any
        # further row of the same kind carries 1 rather than repeating it.
        assigned: list[tuple[list[CorporateAction], Decimal]] = [
            (by_kind.get(CorporateActionType.CASH_DIVIDEND, []), cash_factor),
            (by_kind.get(CorporateActionType.RIGHTS_ISSUE, []), rights_factor),
            ([a for k in DILUTIVE for a in by_kind.get(k, [])], dilution_factor),
            (by_kind.get(CorporateActionType.CAPITAL_REDUCTION, []), reduction_factor),
            (by_kind.get(CorporateActionType.PAR_VALUE_CHANGE, []), ONE),
        ]
        for rows, share in assigned:
            for n, row in enumerate(rows):
                day.components[row.id] = share if n == 0 else ONE

        self._reconcile(day, actions)
        return day

    @staticmethod
    def _reconcile(day: DayFactor, actions: list[CorporateAction]) -> None:
        before = _positive_ref(actions, "reference_price_before")
        after = _positive_ref(actions, "reference_price_after")
        if before is None or after is None:
            return
        day.published = after / before
        day.residual_bps = abs(day.factor - day.published) / day.published * Decimal(10000)
        if day.residual_bps > RECONCILIATION_TOLERANCE_BPS:
            log.warning(
                "adjustment_reconciliation_mismatch",
                symbol=day.symbol,
                ex_date=str(day.ex_date),
                computed=str(round(day.factor, 8)),
                published=str(round(day.published, 8)),
                residual_bps=str(round(day.residual_bps, 2)),
            )

    # ------------------------------------------------------------ computing
    async def compute_factors(
        self,
        symbols: list[str],
        *,
        after: date,
        market: str = "TWSE",
        as_of: datetime | None = None,
        persist: bool = True,
    ) -> list[DayFactor]:
        """Every day factor for these symbols with an ex-date after `after`.

        Persisting puts each action's share of its day onto the action row, which
        is what makes an adjusted price explainable: the chain from a published
        dividend to a changed close becomes one join rather than a rerun of this
        code.
        """
        grouped = await self._actions(symbols, after, market, as_of)
        if not grouped:
            return []

        closes = await self._prior_closes(set(grouped), market)
        factors = [
            self.compute_day_factor(actions, prior_close=closes.get(key))
            for key, actions in sorted(grouped.items())
        ]

        if persist:
            for day in factors:
                for action_id, share in day.components.items():
                    await self.session.execute(
                        update(CorporateAction)
                        .where(CorporateAction.id == action_id)
                        .values(factor=share)
                    )
            await self.session.flush()
        return factors

    # ------------------------------------------------------------- applying
    async def apply(
        self,
        symbols: list[str],
        *,
        start: date,
        end: date,
        market: str = "TWSE",
        require_coverage: bool = True,
    ) -> AdjustmentReport:
        """Write `adjust_factor` and `adjusted_close` over [start, end].

        The series is anchored on the present: a bar's factor is the product of
        every day factor after it, so the most recent bar is unadjusted and
        history is restated in today's terms. That is the convention Taiwanese
        vendors use, and matching it lets a chart here and a chart there be
        compared without first asking which end was pinned.

        Events after `end` are therefore included. Stopping at `end` would anchor
        each window separately, and two overlapping requests would disagree about
        the same historical day.
        """
        if require_coverage:
            # Before any arithmetic. Adjusting a symbol nobody has fetched
            # actions for produces an unadjusted series indistinguishable from a
            # correct one.
            await self.coverage.assert_adjustable(symbols, start, end, market)

        factors = await self.compute_factors(symbols, after=start, market=market)
        blocked = {f.symbol for f in factors if not f.resolved}

        by_symbol: dict[str, list[DayFactor]] = {}
        for f in factors:
            if f.symbol not in blocked:
                by_symbol.setdefault(f.symbol, []).append(f)

        now = datetime.now(UTC)
        rows_written = 0
        for symbol in symbols:
            if symbol in blocked:
                continue
            rows_written += await self._write_series(
                symbol, market, start, end, by_symbol.get(symbol, []), now
            )

        report = AdjustmentReport(
            symbols=len(symbols),
            adjusted=len(symbols) - len(blocked),
            blocked=sorted(blocked),
            events=len(factors),
            unresolved=[f for f in factors if not f.resolved],
            mismatched=[f for f in factors if f.reconciled is False],
            rows_written=rows_written,
        )
        log.info(
            "adjustment_applied",
            symbols=len(symbols),
            blocked=len(blocked),
            events=len(factors),
            rows=rows_written,
        )
        return report

    async def _write_series(
        self,
        symbol: str,
        market: str,
        start: date,
        end: date,
        events: list[DayFactor],
        now: datetime,
    ) -> int:
        """Set the cumulative factor on every bar in the window.

        Bars after the last ex-date are written with a factor of exactly 1 rather
        than left null. Null means "never computed", and a series whose recent
        rows are null while its older rows are adjusted is one nobody can read
        without knowing this function.
        """
        written = 0
        upper = end
        cumulative = ONE

        for f in sorted(events, key=lambda e: e.ex_date, reverse=True):
            lower = f.ex_date
            if lower <= upper:
                written += await self._write_segment(
                    symbol, market, max(lower, start), upper, cumulative, now
                )
            upper = f.ex_date - ONE_DAY
            cumulative *= f.factor
            if upper < start:
                break

        if upper >= start:
            written += await self._write_segment(symbol, market, start, upper, cumulative, now)
        return written

    async def _write_segment(
        self,
        symbol: str,
        market: str,
        lower: date,
        upper: date,
        factor: Decimal,
        now: datetime,
    ) -> int:
        # `AsyncSession.execute` is typed as returning `Result`, which has no
        # row count; an UPDATE really returns a `CursorResult`, which does.
        result = cast(
            "CursorResult[Any]",
            await self.session.execute(
                update(DailyPrice)
                .where(
                    DailyPrice.symbol == symbol,
                    DailyPrice.market == market,
                    DailyPrice.trading_date >= lower,
                    DailyPrice.trading_date <= upper,
                )
                .values(
                    adjust_factor=factor,
                    adjusted_close=DailyPrice.close * factor,
                    adjusted_at=now,
                )
            ),
        )
        return int(result.rowcount or 0)


__all__ = [
    "MAX_FACTOR",
    "MIN_FACTOR",
    "RECONCILIATION_TOLERANCE_BPS",
    "AdjustmentReport",
    "AdjustmentService",
    "DayFactor",
]
