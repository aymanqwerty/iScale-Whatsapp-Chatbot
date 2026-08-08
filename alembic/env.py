"""Alembic environment.

Reads the database URL from application settings rather than `alembic.ini`, so
migrations always target the same database as the app. Runs the async engine
through `connection.run_sync`, which is how Alembic (a synchronous library)
drives an asyncpg connection.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing the package registers every model on Base.metadata; without this,
# autogenerate would produce an empty migration.
import app.db.models  # noqa: F401
from alembic import context
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import engine_kwargs

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
# Alembic keeps its config in a `configparser`, where `%` starts an
# interpolation token. A percent-encoded character in the URL - which any
# password containing `@`, `/` or `:` must have, e.g. `p@ss` -> `p%40ss` - then
# raises "invalid interpolation syntax" before a single migration runs.
# Doubling the sign escapes it; configparser stores the original value.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata


def _include_object(obj: object, name: str | None, type_: str, *args: object) -> bool:
    """Hook for excluding tables Alembic should not manage."""
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting - `alembic upgrade head --sql`."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Alembic builds its own engine, so the pooler handling in
    # `app.db.session` does not apply here. Behind a transaction-mode pooler
    # (Supabase, PgBouncer) asyncpg's prepared-statement cache raises
    # `DuplicatePreparedStatementError` before the first migration runs -
    # SQLAlchemy's own dialect startup issues enough statements to collide.
    # Reusing the same decision keeps the two paths from drifting apart.
    connect_args = engine_kwargs(settings).get("connect_args", {})

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
