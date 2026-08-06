"""FastAPI dependencies.

Everything resolves from the `Container` built at startup and stored on
`app.state`, so no module-level globals and no per-request construction of
clients.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.core.config import Settings
from app.services.conversation_service import ConversationService


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
