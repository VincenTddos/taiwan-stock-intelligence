from fastapi import APIRouter

from app.api.v1 import auth, health, market, meta

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(meta.router)
api_router.include_router(market.router)
