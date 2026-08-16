"""The Data Contract Rule and the no-fake-data rule, enforced at the API edge."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

CONTRACT_NAMES = {
    "MarketQuote",
    "HistoricalPrice",
    "FinancialFact",
    "NewsDocument",
    "InstitutionalFlow",
    "FactorScore",
    "AIStockScore",
    "Provenance",
}


async def test_contracts_are_published(client):
    schemas = (await client.get("/api/v1/meta/contracts")).json()["data"]
    assert CONTRACT_NAMES.issubset(schemas.keys())


async def test_financial_fact_schema_requires_bitemporal_fields(client):
    schema = (await client.get("/api/v1/meta/contracts")).json()["data"]["FinancialFact"]
    assert "period_end" in schema["required"]
    assert "announced_at" in schema["required"]


async def test_provenance_schema_has_reproducibility_triple(client):
    schema = (await client.get("/api/v1/meta/contracts")).json()["data"]["Provenance"]
    for field in (
        "model_version",
        "dataset_version",
        "feature_version",
        "calculated_at",
        "data_as_of",
    ):
        assert field in schema["properties"], f"{field} missing from Provenance"
    assert "calculated_at" in schema["required"]
    assert "data_as_of" in schema["required"]


async def test_capabilities_declares_only_what_is_built(client):
    """Phase 2 delivers market data. Everything downstream must still be
    declared absent — the capability endpoint is the honest answer to
    'what can this deployment actually do?'."""
    caps = (await client.get("/api/v1/meta/capabilities")).json()["data"]
    features = caps["features"]
    assert features["market_data"] is True
    for f in ("quant", "news", "ai_score", "backtest", "portfolio"):
        assert features[f] is False, f"Phase 2 must not claim feature '{f}'"


async def test_no_endpoint_serves_unbuilt_features(client):
    """Guards against a well-meaning demo endpoint appearing ahead of its phase."""
    spec = (await client.get("/api/v1/openapi.json")).json()
    forbidden = ("/ai-score", "/news", "/backtest", "/sectors", "/copilot", "/portfolio")
    offenders = [p for p in spec["paths"] if any(p.startswith(f"/api/v1{x}") for x in forbidden)]
    assert offenders == [], f"endpoints exist for unbuilt features: {offenders}"


async def test_market_endpoints_are_registered(client):
    spec = (await client.get("/api/v1/openapi.json")).json()
    for path in (
        "/api/v1/market/status",
        "/api/v1/stocks",
        "/api/v1/stocks/{symbol}/prices",
        "/api/v1/indices",
        "/api/v1/institutional",
    ):
        assert path in spec["paths"], path


async def test_openapi_is_generated(client):
    spec = (await client.get("/api/v1/openapi.json")).json()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "twquant API"
