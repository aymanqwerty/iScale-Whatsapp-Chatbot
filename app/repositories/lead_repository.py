"""Persistence for `Lead`."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.lead import Lead
from app.domain.enums import LeadStatus, LeadType, SyncStatus


class LeadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: int,
        conversation_id: int | None,
        lead_type: LeadType,
        phone: str,
        name: str | None = None,
        interested_course: str | None = None,
        preferred_time: datetime | None = None,
        preferred_time_raw: str | None = None,
        remarks: str | None = None,
    ) -> Lead:
        lead = Lead(
            user_id=user_id,
            conversation_id=conversation_id,
            type=lead_type,
            status=LeadStatus.NEW,
            phone=phone,
            name=name,
            interested_course=interested_course,
            preferred_time=preferred_time,
            preferred_time_raw=preferred_time_raw,
            remarks=remarks,
            sync_status=SyncStatus.PENDING,
        )
        self._session.add(lead)
        await self._session.flush()
        return lead

    async def get_by_id(self, lead_id: int) -> Lead | None:
        return await self._session.get(Lead, lead_id)

    async def list_recent(self, limit: int = 50, offset: int = 0) -> list[Lead]:
        result = await self._session.execute(
            select(Lead).order_by(Lead.id.desc()).limit(limit).offset(offset)
        )
        return list(result.scalars().all())

    async def list_pending_sync(self, limit: int = 100) -> list[Lead]:
        """Leads that never made it to the sheet - lets an operator retry.

        SKIPPED counts as pending. It means no sink was configured when the lead
        was captured, which is the normal state during development - so every
        lead taken before Google Sheets was switched on carries it. Excluding
        them made those leads permanently unrecoverable through this path, even
        once a sink existed. Only SYNCED is terminal.
        """
        result = await self._session.execute(
            select(Lead)
            .where(
                Lead.sync_status.in_(
                    (SyncStatus.PENDING, SyncStatus.FAILED, SyncStatus.SKIPPED)
                )
            )
            .order_by(Lead.id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def mark_synced(self, lead: Lead) -> Lead:
        lead.sync_status = SyncStatus.SYNCED
        lead.synced_at = datetime.now(UTC)
        lead.sync_error = None
        await self._session.flush()
        return lead

    async def mark_sync_failed(self, lead: Lead, error: str) -> Lead:
        lead.sync_status = SyncStatus.FAILED
        lead.sync_error = error[:2000]
        await self._session.flush()
        return lead

    async def mark_sync_skipped(self, lead: Lead) -> Lead:
        lead.sync_status = SyncStatus.SKIPPED
        await self._session.flush()
        return lead

    async def find_upcoming_callback(self, phone: str) -> Lead | None:
        """The most recent still-open callback booked for this number.

        "Open" means a counselor has not yet worked it (`NEW`) and the slot has
        not already passed. A lead that is CONTACTED or in the past is history:
        rescheduling it would rewrite a call that already happened.
        """
        result = await self._session.execute(
            select(Lead)
            .where(
                Lead.phone == phone,
                Lead.status == LeadStatus.NEW,
                Lead.preferred_time.is_not(None),
                Lead.preferred_time >= datetime.now(UTC),
            )
            .order_by(Lead.preferred_time.asc())
            .limit(1)
        )
        return result.scalars().first()

    async def reschedule(
        self,
        lead: Lead,
        *,
        preferred_time: datetime,
        preferred_time_raw: str | None = None,
    ) -> Lead:
        """Move an existing booking to a new slot.

        The row is updated rather than replaced so the counselor sees one entry
        with the correct time - a second row showing the old slot is exactly the
        confusion a reschedule is meant to remove. `sync_status` returns to
        PENDING so the sheet is brought back into line.
        """
        lead.preferred_time = preferred_time
        if preferred_time_raw is not None:
            lead.preferred_time_raw = preferred_time_raw
        lead.sync_status = SyncStatus.PENDING
        lead.synced_at = None
        lead.sync_error = None
        await self._session.flush()
        return lead
