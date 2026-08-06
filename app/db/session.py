"""Async engine / session management.

Two ways to obtain a session:

* `get_session()` - a FastAPI dependency, one session per request.
* `session_scope()` - an async context manager for background tasks, which run
  outside the request lifecycle and therefore cannot use the dependency.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Database:
    """Owns the engine and the sessionmaker for the process lifetime."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        kwargs: dict[str, object] = {
            "echo": settings.db_echo,
            "pool_pre_ping": True,
            "future": True,
        }
        # SQLite (used by the tests) has no connection pool sizing knobs.
        if not settings.database_url.startswith("sqlite"):
            kwargs["pool_size"] = settings.db_pool_size
            kwargs["max_overflow"] = settings.db_max_overflow
            kwargs["pool_recycle"] = 1800

        self._engine: AsyncEngine = create_async_engine(settings.database_url, **kwargs)
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        return self._sessionmaker

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional scope: commit on success, roll back on failure."""
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self._engine.dispose()
        logger.info("Database engine disposed")
