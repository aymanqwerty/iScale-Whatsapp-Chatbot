"""Lead destinations (CRM / spreadsheet)."""

from app.services.crm.base import LeadRecord, LeadSink
from app.services.crm.google_sheets import GoogleSheetsLeadSink
from app.services.crm.null_sink import NullLeadSink

__all__ = ["GoogleSheetsLeadSink", "LeadRecord", "LeadSink", "NullLeadSink"]
