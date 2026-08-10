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
#: Columns shared by both tabs, in order. "Lead ID" is first and rarely
#: interesting to a human, but it is what lets a rescheduled call update its own
#: row instead of appending a second one showing the old time.
_COMMON_LEADING: tuple[str, ...] = (
    "Lead ID",
    "Date",
    "WhatsApp",
    "Phone",
    "Name",
)

_COMMON_TRAILING: tuple[str, ...] = (
    "Callback Date",
    "Callback Time",
    "Callback (as asked)",
    "Remarks",
    "Status",
)

#: Pre-sales tab. Profession is the pre-sales-only column that matters: it is
#: what the discovery branch learned, and it is what lets a counselor open the
#: call already knowing who they are talking to.
PRE_SALES_COLUMNS: tuple[str, ...] = (
    *_COMMON_LEADING,
    "Profession",
    "Interested Course",
    *_COMMON_TRAILING,
)

#: Post-sales tab. Email and Enrolled Course are mandatory before a support call
#: is booked at all, so they can never be blank on a row that reached this tab.
POST_SALES_COLUMNS: tuple[str, ...] = (
    *_COMMON_LEADING,
    "Email",
    "Enrolled Course",
    "Issue Type",
    *_COMMON_TRAILING,
)

#: Retained so the legacy single-tab sheet and anything importing this name keep
#: working. New code should ask `columns_for(lead_type)` instead.
LEAD_COLUMNS: tuple[str, ...] = PRE_SALES_COLUMNS


def columns_for(lead_type: str) -> tuple[str, ...]:
    """Header for the tab this lead belongs on."""
    return POST_SALES_COLUMNS if _is_post_sales(lead_type) else PRE_SALES_COLUMNS


def _is_post_sales(lead_type: str) -> bool:
    return "POST" in str(lead_type).upper()


@dataclass(frozen=True, slots=True)
class LeadRecord:
    """One exportable lead."""

    created_at: datetime
    #: The WhatsApp thread the lead came from.
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
    #: The number to actually ring. Falls back to `phone` in `as_row` when not
    #: supplied, so the column is never blank on a row a counselor must act on.
    contact_phone: str = ""
    email: str = ""
    enrolled_course: str = ""
    profession: str = ""
    issue_type: str = ""

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
            contact_phone=lead.contact_phone or "",
            email=lead.email or "",
            enrolled_course=lead.enrolled_course or "",
            profession=lead.profession or "",
            issue_type=lead.issue_type or "",
        )

    @property
    def is_post_sales(self) -> bool:
        return _is_post_sales(self.lead_type)

    @property
    def columns(self) -> tuple[str, ...]:
        """Header of the tab this record belongs on."""
        return columns_for(self.lead_type)

    def _values(self) -> dict[str, str]:
        """Every column this record can fill, keyed by header name."""
        return {
            "Lead ID": str(self.lead_id or ""),
            "Date": self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "WhatsApp": self.phone,
            # Falls back to the WhatsApp number: a blank "Phone" on a booked
            # call is a row nobody can act on.
            "Phone": self.contact_phone or self.phone,
            "Name": self.name,
            "Lead Type": self.lead_type,
            "Profession": self.profession,
            "Interested Course": self.interested_course,
            "Email": self.email,
            "Enrolled Course": self.enrolled_course,
            "Issue Type": self.issue_type,
            "Callback Date": self.callback_date,
            "Callback Time": self.callback_time,
            "Callback (as asked)": self.callback_raw,
            "Remarks": self.remarks,
            "Status": self.status,
        }

    def as_row(self, columns: tuple[str, ...] | None = None) -> list[str]:
        """Values in the given header's order, defaulting to this lead's tab.

        Built by looking each header up by name rather than by position, so a
        column added to one tab cannot silently shift every value on the other.
        Written with `valueInputOption=USER_ENTERED`, so Sheets parses the date
        and time cells into real date/time values rather than storing text.
        """
        values = self._values()
        return [values.get(column, "") for column in (columns or self.columns)]


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
