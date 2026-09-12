"""Endpoints driven by a scheduler rather than by a person or a customer.

Cloud Run's request-based billing allocates CPU only while a request is in
flight, so anything that has to happen on a timer cannot be a background loop -
it would tick only while a message happened to be in flight, which is exactly
when nobody is idle enough to need chasing. Cloud Scheduler calls in instead,
and the work runs inside a request where it has CPU.

Behind the same `X-API-Key` as the lead endpoints. Triggering a sweep sends real
WhatsApp messages to real customers, so it is not something an anonymous caller
who finds the URL should be able to do.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_api_key
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/internal", tags=["internal"], dependencies=[Depends(require_api_key)]
)


@router.post("/sweep", summary="Run one inactivity sweep")
async def sweep(request: Request) -> dict[str, Any]:
    """Nudge conversations that have gone quiet, and report how many.

    Safe to call more often than needed and safe to call twice: the sweep reads
    each conversation's own nudge stage and skips anything already chased, so
    frequency changes when a follow-up goes out, never whether it goes out
    twice.

    Never raises. A scheduler that sees a 500 retries, and a retried sweep that
    failed halfway would re-send to whoever it had already reached.
    """
    sweeper = getattr(request.app.state, "sweeper", None)
    if sweeper is None:  # pragma: no cover - lifespan always sets this
        logger.error("Sweep requested but no sweeper is configured")
        return {"status": "unavailable", "nudged": 0}

    try:
        nudged = await sweeper.sweep()
    except Exception:
        logger.exception("Scheduled inactivity sweep failed")
        return {"status": "failed", "nudged": 0}

    return {"status": "ok", "nudged": nudged}
