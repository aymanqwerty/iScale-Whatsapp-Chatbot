"""Liveness and readiness endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.deps import ContainerDep
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Cheap check - the process is up. Never touches a dependency."""
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe")
async def readiness(container: ContainerDep, response: Response) -> dict[str, Any]:
    """Reports each dependency. 503 when the database is unreachable.

    Only the database is treated as fatal: the bot can still greet users, run
    the menus and capture leads with Groq or Sheets degraded, so those are
    reported without failing the probe.
    """
    checks: dict[str, Any] = {}

    try:
        async with container.database.session() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
        database_ok = True
    except Exception as exc:
        logger.error("Database readiness check failed", extra={"error": str(exc)})
        checks["database"] = "error"
        database_ok = False

    checks["knowledge_base"] = {
        "snippets": len(container.knowledge_base),
        "courses": len(container.knowledge_base.courses),
    }
    checks["groq"] = "ok" if await container.llm.health_check() else "unconfigured"
    checks["whatsapp"] = "enabled" if container.settings.whatsapp_enabled else "disabled"
    checks["lead_sink"] = {
        "name": container.lead_sink.name,
        "enabled": container.lead_sink.enabled,
    }

    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if database_ok else "degraded", "checks": checks}
