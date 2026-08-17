from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.api.v1.router import api_router
from app.core.cache import close_redis
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine

settings = get_settings()
configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    log.info(
        "startup",
        app=settings.APP_NAME,
        version=settings.APP_VERSION,
        env=settings.APP_ENV.value,
        llm_enabled=settings.ENABLE_LLM,
    )
    yield
    await close_redis()
    await dispose_engine()
    log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="twquant API",
        description=(
            "AI Taiwan Stock Intelligence Platform.\n\n"
            "**Phase 2 — Market Data Infrastructure.** Serves the trading "
            "calendar, security master, daily prices, index quotes and "
            "institutional flow. No scores, predictions, backtests or news: "
            "those arrive in later phases and no endpoint here pretends "
            "otherwise.\n\n"
            "Every response carries a `meta` block with data provenance — "
            "source, trading date, freshness and whether the value came from "
            "cache. Model-derived values will additionally carry version and "
            "confidence fields. Data that does not exist is reported as "
            "absent, never as zero."
        ),
        version=settings.APP_VERSION,
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Response-Time-ms"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    return app


app = create_app()
