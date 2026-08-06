"""Lead sink abstraction.

`LeadSink` is the seam for swapping Google Sheets for HubSpot (or writing to
both). `LeadRecord` is intentionally a flat, primitive-only structure: it is the
export contract, and keeping ORM objects out of it means a future CRM adapter
never has to understand our database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.db.models.lead import Lead

#: Sheet header row. Order defines the column order everywhere.
LEAD_COLUMNS: tuple[str, ...] = (
    "Date",
    "Phone",
    "Name",
    "Lead Type",
    "Interested Course",
    "Preferred Callback Time",
    "Remarks",
    "Status",
)


@dataclass(frozen=True, slots=True)
class LeadRecord:
    """One exportable lead."""

    created_at: datetime
    phone: str
    name: str
    lead_type: str
    interested_course: str
    preferred_time: str
    remarks: str
    status: str
    lead_id: int | None = None

    @classmethod
    def from_model(cls, lead: Lead, *, timezone_name: str = "UTC") -> LeadRecord:
        """Build an export record from the persisted lead."""
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(timezone_name)
        created = lead.created_at.astimezone(tz) if lead.created_at else datetime.now(tz)

        if lead.preferred_time is not None:
            preferred = lead.preferred_time.astimezone(tz).strftime("%Y-%m-%d %H:%M")
            if lead.preferred_time_raw:
                preferred = f"{preferred} ({lead.preferred_time_raw})"
        else:
            preferred = lead.preferred_time_raw or ""

        return cls(
            created_at=created,
            phone=lead.phone,
            name=lead.name or "",
            lead_type=str(lead.type),
            interested_course=lead.interested_course or "",
            preferred_time=preferred,
            remarks=lead.remarks or "",
            status=str(lead.status),
            lead_id=lead.id,
        )

    def as_row(self) -> list[str]:
        """Values in `LEAD_COLUMNS` order."""
        return [
            self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            self.phone,
            self.name,
            self.lead_type,
            self.interested_course,
            self.preferred_time,
            self.remarks,
            self.status,
        ]


@runtime_checkable
class LeadSink(Protocol):
    """Somewhere a lead gets delivered for humans to act on."""

    @property
    def name(self) -> str:
        """Short identifier used in logs."""
        ...

    @property
    def enabled(self) -> bool:
        """False when the integration is switched off or unconfigured."""
        ...

    async def push_lead(self, record: LeadRecord) -> None:
        """Deliver the lead, or raise `CRMError`."""
        ...

    async def health_check(self) -> bool:
        ...
