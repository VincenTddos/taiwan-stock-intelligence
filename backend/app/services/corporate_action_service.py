"""Corporate action coverage, and the guard that stands in front of adjustment.

Phase 3 computes adjusted prices, moving averages, momentum and return series.
Every one of those reads a price series as if it were continuous. It is not: on
an ex-dividend or ex-rights date the raw close drops by an amount that has
nothing to do with anyone's opinion of the company, and an unadjusted series
records that as a real move.

The failure mode is not that the numbers look wrong. It is that they look
entirely reasonable. A 3% ex-dividend gap is an ordinary day's move; it will not
trip any anomaly rule, it will not look odd on a chart, and it will quietly
appear in a momentum factor as signal. Aggregated over a universe, ex-dates
cluster seasonally, so the contamination is not even noise — it has structure,
and a backtest will happily find it.

So adjustment is gated rather than attempted. `assert_adjustable` refuses to let
a series be adjusted over a range where corporate action coverage has not been
established, and the refusal names what is missing. The alternative — adjusting
with whatever actions happen to be in the table — treats "we have not looked" as
"nothing happened", which is the single most expensive assumption available
here.

This module deliberately contains no adjustment arithmetic and no factor
computation. It answers one question: *is it legitimate to adjust this symbol
over this range yet.*
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.market import CorporateActionCoverage, CorporateActionType

log = get_logger(__name__)

#: The action classes that move a price and therefore must be covered before a
#: series may be adjusted. `PAR_VALUE_CHANGE` is excluded: it alters the stated
#: par value without changing what a holder owns, so it does not enter the
#: factor. It remains in the schema because it belongs in the record of what
#: happened.
PRICE_AFFECTING: frozenset[CorporateActionType] = frozenset(
    {
        CorporateActionType.CASH_DIVIDEND,
        CorporateActionType.STOCK_DIVIDEND,
        CorporateActionType.SPLIT,
        CorporateActionType.REVERSE_SPLIT,
        CorporateActionType.RIGHTS_ISSUE,
        CorporateActionType.CAPITAL_REDUCTION,
    }
)


class AdjustmentNotPermitted(RuntimeError):
    """Raised when a price series may not legitimately be adjusted yet.

    A hard error, not a warning. A warning would be logged, ignored, and the
    contaminated series would be used anyway — which is the outcome this whole
    mechanism exists to prevent.
    """


@dataclass(slots=True)
class CoverageGap:
    symbol: str
    market: str
    reason: str
    requested: tuple[date, date]
    covered: tuple[date, date] | None = None
    missing_action_types: frozenset[str] = frozenset()

    def describe(self) -> str:
        span = f"{self.requested[0]} → {self.requested[1]}"
        if self.covered:
            have = f"{self.covered[0]} → {self.covered[1]}"
            base = f"{self.symbol}: {self.reason} (requested {span}, covered {have})"
        else:
            base = f"{self.symbol}: {self.reason} (requested {span})"
        if self.missing_action_types:
            base += f"; uncovered action types: {sorted(self.missing_action_types)}"
        return base


class CorporateActionCoverageService:
    """Records what has been searched for, and gates adjustment on it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    async def record(
        self,
        *,
        symbol: str,
        market: str,
        source: str,
        covered_from: date,
        covered_to: date,
        action_types: frozenset[CorporateActionType],
        actions_found: int,
        ingestion_id: int | None = None,
    ) -> None:
        """Assert that `source` was searched for `action_types` over the range.

        Ranges widen rather than replace. A later run covering 2024–2026 must not
        erase the fact that an earlier one covered 2019–2024, or a backfill would
        appear to destroy its own history every time it advanced.
        """
        stmt = pg_insert(CorporateActionCoverage).values(
            symbol=symbol,
            market=market,
            source=source,
            covered_from=covered_from,
            covered_to=covered_to,
            action_types=sorted(str(a) for a in action_types),
            actions_found=actions_found,
            verified_at=datetime.now(UTC),
            ingestion_id=ingestion_id,
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_corporate_action_coverage_key",
                set_={
                    # least/greatest, not the excluded value: a later run
                    # covering 2024-2026 must not erase an earlier one that
                    # covered 2019-2024.
                    "covered_from": func.least(
                        CorporateActionCoverage.covered_from, stmt.excluded.covered_from
                    ),
                    "covered_to": func.greatest(
                        CorporateActionCoverage.covered_to, stmt.excluded.covered_to
                    ),
                    "action_types": stmt.excluded.action_types,
                    "actions_found": stmt.excluded.actions_found,
                    "verified_at": stmt.excluded.verified_at,
                    "ingestion_id": stmt.excluded.ingestion_id,
                },
            )
        )

    async def coverage_for(
        self, symbol: str, market: str = "TWSE"
    ) -> list[CorporateActionCoverage]:
        stmt = select(CorporateActionCoverage).where(
            CorporateActionCoverage.symbol == symbol,
            CorporateActionCoverage.market == market,
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------
    async def gaps(
        self,
        symbols: list[str],
        start: date,
        end: date,
        market: str = "TWSE",
        required: frozenset[CorporateActionType] = PRICE_AFFECTING,
    ) -> list[CoverageGap]:
        """Every reason the requested symbols may not be adjusted. Empty is a pass."""
        found: list[CoverageGap] = []
        needed = {str(a) for a in required}

        for symbol in symbols:
            rows = await self.coverage_for(symbol, market)
            if not rows:
                found.append(
                    CoverageGap(
                        symbol=symbol,
                        market=market,
                        reason="no corporate action coverage recorded",
                        requested=(start, end),
                        missing_action_types=frozenset(needed),
                    )
                )
                continue

            # Union across sources: one source may cover dividends and another
            # capital reductions, and together they can be complete.
            covered_from = min(r.covered_from for r in rows)
            covered_to = max(r.covered_to for r in rows)
            covered_types = {t for r in rows for t in r.action_types}
            missing = needed - covered_types

            if covered_from > start or covered_to < end:
                found.append(
                    CoverageGap(
                        symbol=symbol,
                        market=market,
                        reason="coverage does not span the requested range",
                        requested=(start, end),
                        covered=(covered_from, covered_to),
                        missing_action_types=frozenset(missing),
                    )
                )
            elif missing:
                found.append(
                    CoverageGap(
                        symbol=symbol,
                        market=market,
                        reason="some price-affecting action types were never searched for",
                        requested=(start, end),
                        covered=(covered_from, covered_to),
                        missing_action_types=frozenset(missing),
                    )
                )
        return found

    async def assert_adjustable(
        self,
        symbols: list[str],
        start: date,
        end: date,
        market: str = "TWSE",
        required: frozenset[CorporateActionType] = PRICE_AFFECTING,
    ) -> None:
        """Raise unless every symbol may legitimately be adjusted over the range.

        Phase 3's adjusted price and return pipeline calls this first. Anything
        that computes a return, a moving average or a momentum value over a raw
        series without passing here is reading ex-dividend gaps as returns.
        """
        problems = await self.gaps(symbols, start, end, market, required)
        if not problems:
            return

        log.warning(
            "adjustment_blocked",
            symbols=len(symbols),
            blocked=len(problems),
            start=str(start),
            end=str(end),
        )
        shown = "\n  ".join(p.describe() for p in problems[:10])
        more = f"\n  … and {len(problems) - 10} more" if len(problems) > 10 else ""
        raise AdjustmentNotPermitted(
            f"{len(problems)} of {len(symbols)} symbols cannot be adjusted over "
            f"{start} → {end}:\n  {shown}{more}\n"
            f"Ingest corporate actions for these symbols first. Adjusting without "
            f"coverage would treat 'not searched' as 'nothing happened'."
        )


__all__ = [
    "PRICE_AFFECTING",
    "AdjustmentNotPermitted",
    "CorporateActionCoverageService",
    "CoverageGap",
]
