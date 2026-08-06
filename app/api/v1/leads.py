"""Read-only lead endpoints.

Enough for a counselor-facing dashboard to be built on top later. There is no
authentication here yet - keep this router behind your ingress or add an API-key
dependency before exposing it publicly.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import ContainerDep, SessionDep
from app.repositories.lead_repository import LeadRepository
from app.schemas.lead import LeadList, LeadRead

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("", response_model=LeadList, summary="List recent leads")
async def list_leads(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeadList:
    leads = await LeadRepository(session).list_recent(limit=limit, offset=offset)
    return LeadList(
        items=[LeadRead.model_validate(lead) for lead in leads], count=len(leads)
    )


@router.get("/{lead_id}", response_model=LeadRead, summary="Fetch one lead")
async def get_lead(lead_id: int, session: SessionDep) -> LeadRead:
    lead = await LeadRepository(session).get_by_id(lead_id)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    return LeadRead.model_validate(lead)


@router.post("/sync-pending", summary="Retry leads that never reached the CRM")
async def sync_pending(container: ContainerDep) -> dict[str, int]:
    """Manual retry hook for leads whose sheet append failed.

    A background worker can call this on a schedule once one exists.
    """
    synced = await container.lead_sync.retry_pending()
    return {"synced": synced}
