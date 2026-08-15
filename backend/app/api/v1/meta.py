"""Introspection endpoints.

`/meta/contracts` exists so the Data Contract Rule is *observable*: the
frontend, and any future service, can fetch the canonical JSON Schema for every
shared model instead of hand-copying field lists. If backend and frontend ever
disagree about what a `FinancialFact` is, this endpoint is the tie-breaker.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import SettingsDep
from app.schemas import contracts as c
from app.schemas.envelope import Envelope, envelope

router = APIRouter(prefix="/meta", tags=["meta"])

CONTRACT_MODELS: dict[str, type[BaseModel]] = {
    "MarketQuote": c.MarketQuote,
    "HistoricalPrice": c.HistoricalPrice,
    "FinancialFact": c.FinancialFact,
    "NewsDocument": c.NewsDocument,
    "InstitutionalFlow": c.InstitutionalFlow,
    "FactorScore": c.FactorScore,
    "AIStockScore": c.AIStockScore,
    "Provenance": c.Provenance,
}


@router.get("/contracts", response_model=Envelope[dict[str, Any]])
async def contracts() -> Envelope[dict[str, Any]]:
    return envelope(
        {name: model.model_json_schema() for name, model in CONTRACT_MODELS.items()},
        source=["SELF"],
    )


@router.get("/capabilities", response_model=Envelope[dict[str, Any]])
async def capabilities(settings: SettingsDep) -> Envelope[dict[str, Any]]:
    """What this deployment can actually do right now.

    The frontend uses this to avoid advertising features that are switched off,
    and to render the DEMO DATA banner when mock data is permitted.
    """
    return envelope(
        {
            "environment": settings.APP_ENV.value,
            "version": settings.APP_VERSION,
            "phase": "1 — Foundation",
            "features": {
                "market_data": False,
                "quant": False,
                "news": False,
                "ai_score": False,
                "backtest": False,
                "portfolio": False,
                "copilot": settings.ENABLE_LLM,
            },
            "llm_enabled": settings.ENABLE_LLM,
            "mock_data_allowed": settings.ALLOW_MOCK_DATA,
            "note": (
                "Phase 1 is infrastructure only. No market data exists in this "
                "deployment; no endpoint returns prices, scores or news."
            ),
        },
        source=["SELF"],
    )
