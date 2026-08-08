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
#:
#: The callback slot is deliberately three columns rather than one sentence.
#: A counselor's actual question is "who am I calling today?", which needs a
#: real date value to filter and sort on - a string like
#: "2026-08-09 16:00 (sunday 4 pm)" is inert text to Sheets. Date and time are
#: split so a plain equals-today filter on one column answers it, and the
#: user's own wording is kept alongside for context.
LEAD_COLUMNS: tuple[str, ...] = (
    "Date",
    "Phone",
    "Name",
    "Lead Type",
    "Interested Course",
    "Callback Date",
    "Callback Time",
    "Callback (as asked)",
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
    #: ISO date, e.g. "2026-08-09". Empty when no slot was agreed.
    callback_date: str
    #: 24-hour time, e.g. "16:00".
    callback_time: str
    #: What the user actually typed ("sunday 4 pm"), kept for context.
    callback_raw: str
    remarks: str
    status: str
    lead_id: int | None = None

    @classmethod
    def from_model(cls, lead: Lead, *, timezone_name: str = "UTC") -> LeadRecord:
        """Build an export record from the persisted lead."""
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(timezone_name)
        created = lead.created_at.astimezone(tz) if lead.created_at else datetime.now(tz)

        callback_date = callback_time = ""
        if lead.preferred_time is not None:
            local = lead.preferred_time.astimezone(tz)
            # ISO order is unambiguous in every Sheets locale; d/m/y versus
            # m/d/y would be read differently depending on the sheet's region.
            callback_date = local.strftime("%Y-%m-%d")
            callback_time = local.strftime("%H:%M")

        return cls(
            created_at=created,
            phone=lead.phone,
            name=lead.name or "",
            lead_type=str(lead.type),
            interested_course=lead.interested_course or "",
            callback_date=callback_date,
            callback_time=callback_time,
            callback_raw=lead.preferred_time_raw or "",
            remarks=lead.remarks or "",
            status=str(lead.status),
            lead_id=lead.id,
        )

    def as_row(self) -> list[str]:
        """Values in `LEAD_COLUMNS` order.

        Written with `valueInputOption=USER_ENTERED`, so Sheets parses the date
        and time cells into real date/time values rather than storing text.
        """
        return [
            self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            self.phone,
            self.name,
            self.lead_type,
            self.interested_course,
            self.callback_date,
            self.callback_time,
            self.callback_raw,
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
