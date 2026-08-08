"""FastAPI dependencies.

Everything resolves from the `Container` built at startup and stored on
`app.state`, so no module-level globals and no per-request construction of
clients.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.core.config import Settings
from app.core.logging import get_logger
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)


def get_container(request: Request) -> Container:
    container: Container | None = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - lifespan always sets this
        raise RuntimeError("Application container is not initialised")
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def get_settings(container: ContainerDep) -> Settings:
    return container.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_session(container: ContainerDep) -> AsyncIterator[AsyncSession]:
    """One transactional session per request."""
    async with container.database.session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_conversation_service(container: ContainerDep) -> ConversationService:
    return container.conversation_service()


ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]


def require_api_key(
    container: ContainerDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Guard endpoints that expose lead data.

    An unset key is refused in production rather than warned about: these
    endpoints return names, phone numbers and remarks, and "we logged a warning"
    is not a defence for serving that to the internet. Locally it stays open so
    curl and the test suite are not made tedious.
    """
    expected = container.settings.api_key.get_secret_value()

    if not expected:
        if container.settings.is_production:
            logger.error("API_KEY is not set - refusing to serve lead data")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Lead API is not configured",
            )
        logger.warning(
            "API_KEY is not set - lead endpoints are unauthenticated (local only)"
        )
        return

    # Constant-time compare: a plain `!=` leaks the key one character at a time
    # to anyone who can measure response latency.
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key",
        )


ApiKeyDep = Annotated[None, Depends(require_api_key)]
