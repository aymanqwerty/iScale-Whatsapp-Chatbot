"""API-facing lead schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.domain.enums import LeadStatus, LeadType, SyncStatus


class LeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    conversation_id: int | None
    type: LeadType
    status: LeadStatus
    name: str | None
    phone: str
    interested_course: str | None
    preferred_time: datetime | None
    preferred_time_raw: str | None
    remarks: str | None
    sync_status: SyncStatus
    synced_at: datetime | None
    created_at: datetime


class LeadList(BaseModel):
    items: list[LeadRead]
    count: int
