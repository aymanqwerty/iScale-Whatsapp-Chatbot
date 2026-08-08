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


#: Hostname fragments that mean "a PgBouncer-style transaction pooler".
#: Supabase's pooler is the one we deploy behind; Neon and PgBouncer generally
#: behave the same way.
_POOLER_HINTS = ("pooler.supabase.com", "pgbouncer", "-pooler.")


def _is_transaction_pooler(url: str) -> bool:
    """True when the URL points at a connection pooler rather than Postgres.

    Supabase's pooler runs in transaction mode: a client gets a different
    backend connection per transaction. asyncpg, meanwhile, caches server-side
    prepared statements by name and assumes they persist. Together they produce
    `DuplicatePreparedStatementError` / `InvalidSQLStatementNameError` under
    load - intermittently, only in production, and only once traffic overlaps.
    """
    lowered = url.lower()
    return any(hint in lowered for hint in _POOLER_HINTS) or ":6543/" in lowered


def engine_kwargs(settings: Settings) -> dict[str, object]:
    """Arguments for `create_async_engine`, given the target database.

    Separated from `Database` so the pooler decision can be asserted directly
    rather than by reaching into SQLAlchemy's internals.
    """
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

    if _is_transaction_pooler(settings.database_url):
        # Turning the cache off is the supported way to run asyncpg through a
        # transaction pooler. Costs one extra parse per statement, which is
        # nothing next to the network hop, and removes a whole class of
        # heisenbug.
        kwargs["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        }
    return kwargs


class Database:
    """Owns the engine and the sessionmaker for the process lifetime."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        kwargs = engine_kwargs(settings)
        if "connect_args" in kwargs:
            logger.info(
                "Connection pooler detected - asyncpg prepared-statement "
                "caching disabled (required for transaction-mode pooling)"
            )

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
