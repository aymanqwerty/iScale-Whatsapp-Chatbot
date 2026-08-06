"""No-op lead sink, used when no external CRM is configured."""

from __future__ import annotations

from app.core.logging import get_logger
from app.services.crm.base import LeadRecord

logger = get_logger(__name__)


class NullLeadSink:
    """Records leads to the log only. The database remains the source of truth."""

    def __init__(self) -> None:
        self.pushed: list[LeadRecord] = []

    @property
    def name(self) -> str:
        return "null"

    @property
    def enabled(self) -> bool:
        return False

    async def push_lead(self, record: LeadRecord) -> None:
        self.pushed.append(record)
        logger.info(
            "Lead captured (no CRM sink configured)",
            extra={"lead_id": record.lead_id, "lead_type": record.lead_type},
        )

    async def health_check(self) -> bool:
        return True
