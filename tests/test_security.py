"""API-key protection on lead data, and the rate limits around the webhook."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.ratelimit import SlidingWindowLimiter


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #
def test_limiter_allows_up_to_the_budget() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)

    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]


def test_limiter_is_per_key() -> None:
    """One noisy sender must not throttle everyone else."""
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)

    assert limiter.allow("first")
    assert not limiter.allow("first")
    assert limiter.allow("second")


def test_limiter_window_slides() -> None:
    """Budget must come back once the window passes.

    The sleep is comfortably longer than the window: a margin of a few
    milliseconds makes this fail intermittently on a loaded CI machine, and a
    flaky test is worse than no test.
    """
    limiter = SlidingWindowLimiter(limit=1, window_seconds=0.05)

    assert limiter.allow("a")
    assert not limiter.allow("a")
    time.sleep(0.25)
    assert limiter.allow("a")


def test_limiter_reports_remaining_without_consuming() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)
    limiter.allow("a")

    assert limiter.remaining("a") == 2
    assert limiter.remaining("a") == 2  # unchanged by asking


def test_limiter_evicts_so_it_cannot_leak() -> None:
    """The limiter must not become the memory exhaustion it prevents."""
    limiter = SlidingWindowLimiter(limit=1, window_seconds=0.01, max_keys=10)

    for i in range(200):
        limiter.allow(f"key-{i}")

    assert len(limiter) <= 50, "keys accumulated without bound"


def test_limiter_rejects_a_nonsense_budget() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        SlidingWindowLimiter(limit=0, window_seconds=60)


# --------------------------------------------------------------------------- #
# API key on the lead endpoints
# --------------------------------------------------------------------------- #
def test_leads_require_the_api_key(api_key_client: TestClient) -> None:
    assert api_key_client.get("/api/v1/leads").status_code == 401


def test_leads_reject_a_wrong_api_key(api_key_client: TestClient) -> None:
    response = api_key_client.get("/api/v1/leads", headers={"X-API-Key": "wrong"})

    assert response.status_code == 401


def test_leads_accept_the_right_api_key(api_key_client: TestClient) -> None:
    response = api_key_client.get("/api/v1/leads", headers={"X-API-Key": "s3cret-key"})

    assert response.status_code == 200
    assert "items" in response.json()


def test_every_lead_route_is_protected(api_key_client: TestClient) -> None:
    """Declared on the router, so a new route cannot be left open by oversight."""
    for path in ("/api/v1/leads", "/api/v1/leads/1"):
        assert api_key_client.get(path).status_code == 401
    assert api_key_client.post("/api/v1/leads/sync-pending").status_code == 401


def test_production_refuses_to_serve_leads_without_a_key(
    unconfigured_prod_client: TestClient,
) -> None:
    """An unset key must fail closed in production, not fall open."""
    response = unconfigured_prod_client.get("/api/v1/leads")

    assert response.status_code == 503


# --------------------------------------------------------------------------- #
# Connection-pooler detection (Supabase / PgBouncer)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
        "postgresql+asyncpg://u:p@db-pooler.example.com:5432/postgres",
        "postgresql+asyncpg://u:p@pgbouncer.internal:5432/postgres",
        "postgresql+asyncpg://u:p@host:6543/postgres",
    ],
)
def test_pooler_urls_are_detected(url: str) -> None:
    """Transaction poolers need asyncpg's prepared-statement cache disabled.

    Without it, overlapping requests raise DuplicatePreparedStatementError -
    intermittently, only in production, only once traffic overlaps.
    """
    from app.db.session import _is_transaction_pooler

    assert _is_transaction_pooler(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@db.example.supabase.co:5432/postgres",
        "postgresql+asyncpg://postgres:pw@localhost:5432/iscale",
        "sqlite+aiosqlite:///./test.db",
    ],
)
def test_direct_connections_are_not_treated_as_poolers(url: str) -> None:
    """A direct connection keeps the cache - it is a real performance win."""
    from app.db.session import _is_transaction_pooler

    assert not _is_transaction_pooler(url)


def test_pooler_engine_disables_the_statement_cache() -> None:
    """The setting must actually reach asyncpg, not just be computed."""
    from app.core.config import Settings
    from app.db.session import engine_kwargs

    pooled = engine_kwargs(Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@aws-0.pooler.supabase.com:6543/postgres",
    ))
    assert pooled["connect_args"] == {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }

    direct = engine_kwargs(Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@db.example.supabase.co:5432/postgres",
    ))
    assert "connect_args" not in direct, "a direct connection should keep the cache"
