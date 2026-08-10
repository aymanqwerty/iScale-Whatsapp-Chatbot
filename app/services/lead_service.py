"""Lead creation and CRM synchronisation.

Creation and sync are deliberately separated. A lead is committed to PostgreSQL
first and pushed to Google Sheets afterwards, out of band: the spreadsheet being
slow or down must never cost us the lead or delay the user's confirmation
message. Failed pushes are recorded on the row (`sync_status`), so nothing is
silently lost and a retry job can pick them up.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import CRMError
from app.core.logging import get_logger
from app.db.models.lead import Lead
from app.db.session import Database
from app.domain.enums import LeadType
from app.repositories.lead_repository import LeadRepository
from app.services.crm.base import LeadRecord, LeadSink

logger = get_logger(__name__)


class LeadService:
    """Creates leads inside the caller's transaction."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._repo = LeadRepository(session)

    async def create_lead(
        self,
        *,
        user_id: int,
        conversation_id: int | None,
        lead_type: LeadType,
        phone: str,
        name: str | None,
        interested_course: str | None = None,
        preferred_time: datetime | None = None,
        preferred_time_raw: str | None = None,
        remarks: str | None = None,
        contact_phone: str | None = None,
        email: str | None = None,
        enrolled_course: str | None = None,
        profession: str | None = None,
        issue_type: str | None = None,
    ) -> Lead:
        lead = await self._repo.create(
            user_id=user_id,
            conversation_id=conversation_id,
            lead_type=lead_type,
            phone=phone,
            name=name,
            interested_course=interested_course,
            preferred_time=preferred_time,
            preferred_time_raw=preferred_time_raw,
            remarks=remarks,
            contact_phone=contact_phone,
            email=email,
            enrolled_course=enrolled_course,
            profession=profession,
            issue_type=issue_type,
        )
        logger.info(
            "Lead created",
            extra={
                "lead_id": lead.id,
                "lead_type": str(lead_type),
                "course": interested_course,
                "user_id": user_id,
            },
        )
        return lead


class LeadSyncService:
    """Pushes committed leads to the configured sink.

    Runs outside the request transaction and opens its own session, because it
    is invoked from a background task once the webhook response has been sent.
    """

    def __init__(self, database: Database, sink: LeadSink, settings: Settings) -> None:
        self._database = database
        self._sink = sink
        self._settings = settings

    async def sync(self, lead_id: int) -> bool:
        """Push one lead. Returns True on success; never raises."""
        async with self._database.session() as session:
            repo = LeadRepository(session)
            lead = await repo.get_by_id(lead_id)
            if lead is None:
                logger.warning("Lead vanished before sync", extra={"lead_id": lead_id})
                return False

            if not self._sink.enabled:
                await repo.mark_sync_skipped(lead)
                logger.info(
                    "Lead sync skipped - no sink enabled", extra={"lead_id": lead_id}
                )
                return False

            record = LeadRecord.from_model(lead, timezone_name=self._settings.business_timezone)
            try:
                await self._sink.push_lead(record)
            except CRMError as exc:
                await repo.mark_sync_failed(lead, str(exc))
                logger.error(
                    "Lead sync failed",
                    extra={"lead_id": lead_id, "sink": self._sink.name, "error": str(exc)},
                )
                return False
            except Exception as exc:
                await repo.mark_sync_failed(lead, str(exc))
                logger.exception("Unexpected error syncing lead", extra={"lead_id": lead_id})
                return False

            await repo.mark_synced(lead)
            return True

    async def retry_pending(self, limit: int = 100) -> int:
        """Re-attempt every lead that has not reached the sink yet."""
        async with self._database.session() as session:
            pending = await LeadRepository(session).list_pending_sync(limit=limit)
            lead_ids = [lead.id for lead in pending]

        synced = 0
        for lead_id in lead_ids:
            if await self.sync(lead_id):
                synced += 1
        logger.info(
            "Pending lead sync finished",
            extra={"attempted": len(lead_ids), "synced": synced},
        )
        return synced
