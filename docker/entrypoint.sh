#!/usr/bin/env sh
# Wait for PostgreSQL, apply migrations, then hand over to the CMD.
#
# Running migrations here keeps a single-container deploy simple. If you scale to
# several replicas, move this to a one-shot job so concurrent boots do not race
# on the same migration.
set -eu

echo "[entrypoint] waiting for the database..."
python - <<'PY'
import asyncio
import sys
import time

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import Database

DEADLINE = 60


async def wait() -> None:
    settings = get_settings()
    database = Database(settings)
    started = time.monotonic()
    while True:
        try:
            async with database.session() as session:
                await session.execute(text("SELECT 1"))
            print("[entrypoint] database is ready")
            await database.dispose()
            return
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() - started > DEADLINE:
                print(f"[entrypoint] database unreachable after {DEADLINE}s: {exc}")
                await database.dispose()
                sys.exit(1)
            time.sleep(1)


asyncio.run(wait())
PY

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    echo "[entrypoint] applying migrations..."
    alembic upgrade head
fi

echo "[entrypoint] starting: $*"
exec "$@"
