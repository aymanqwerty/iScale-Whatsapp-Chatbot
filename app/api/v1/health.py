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
# HEAD is registered separately and hidden from the schema: one `api_route`
# with both methods makes FastAPI emit two operations with the same id, which
# it warns about and which pollutes the OpenAPI document.
@router.head("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Cheap check - the process is up. Never touches a dependency.

    HEAD is accepted as well as GET. Uptime monitors (UptimeRobot among them)
    send HEAD by default to avoid transferring a body, and FastAPI - unlike
    plain Starlette - does not add HEAD to a GET route automatically. The
    result was a 405 on every ping: the monitor reported the service down, and
    the keep-alive that stops a free instance sleeping never landed.
    """
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe")
@router.head("/health/ready", include_in_schema=False)
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
    # Keyed by provider, not hard-coded: a readiness probe that always says
    # "groq" while Gemini is answering is worse than no probe at all.
    provider = container.settings.llm_provider
    checks[provider] = "ok" if await container.llm.health_check() else "unconfigured"
    checks["whatsapp"] = "enabled" if container.settings.whatsapp_enabled else "disabled"
    checks["lead_sink"] = {
        "name": container.lead_sink.name,
        "enabled": container.lead_sink.enabled,
    }

    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if database_ok else "degraded", "checks": checks}
