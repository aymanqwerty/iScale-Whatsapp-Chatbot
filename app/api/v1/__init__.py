"""API v1 routers."""

from fastapi import APIRouter

from app.api.v1 import console, health, leads, simulate, webhook

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(webhook.router)
api_router.include_router(leads.router)
api_router.include_router(simulate.router)
api_router.include_router(console.router)

__all__ = ["api_router"]
