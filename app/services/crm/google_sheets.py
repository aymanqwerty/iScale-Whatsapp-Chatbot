"""Google Sheets lead sink.

The Google API client is synchronous, so every call is pushed to a worker
thread with `asyncio.to_thread` - blocking the event loop inside a webhook
handler would stall every other conversation in flight.

Setup: create a service account, download the JSON key, and share the target
spreadsheet with the service account's email address as an Editor.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import Settings
from app.core.exceptions import CRMError
from app.core.logging import get_logger
from app.services.crm.base import LEAD_COLUMNS, LeadRecord

logger = get_logger(__name__)

_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


class GoogleSheetsLeadSink:
    """Appends one row per lead to a worksheet."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._spreadsheet_id = settings.google_sheets_spreadsheet_id
        self._worksheet = settings.google_sheets_worksheet_name
        self._service: Any | None = None
        self._lock = asyncio.Lock()
        self._header_checked = False

    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return "google_sheets"

    @property
    def enabled(self) -> bool:
        return bool(
            self._settings.google_sheets_enabled
            and self._spreadsheet_id
            and self._settings.google_credentials_info()
        )

    # ------------------------------------------------------------------ #
    def _build_service(self) -> Any:
        """Blocking - always called via `asyncio.to_thread`."""
        try:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise CRMError(
                "google-api-python-client is not installed; "
                "run `pip install google-api-python-client google-auth`"
            ) from exc

        info = self._settings.google_credentials_info()
        if info is None:
            raise CRMError("No Google service-account credentials configured")

        credentials = Credentials.from_service_account_info(info, scopes=list(_SCOPES))
        # cache_discovery=False avoids a noisy warning and a needless disk cache.
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)

    async def _ensure_service(self) -> Any:
        if self._service is not None:
            return self._service
        async with self._lock:
            if self._service is None:
                self._service = await asyncio.to_thread(self._build_service)
                logger.info(
                    "Google Sheets client initialised",
                    extra={"spreadsheet_id": self._spreadsheet_id},
                )
        return self._service

    # ------------------------------------------------------------------ #
    async def push_lead(self, record: LeadRecord) -> None:
        if not self.enabled:
            raise CRMError("Google Sheets sink is not enabled or not configured")

        service = await self._ensure_service()
        await self._ensure_header(service)

        try:
            await asyncio.to_thread(self._append_row, service, record.as_row())
        except CRMError:
            raise
        except Exception as exc:
            logger.exception("Failed to append lead to Google Sheets")
            raise CRMError(f"Google Sheets append failed: {exc}") from exc

        logger.info(
            "Lead appended to Google Sheets",
            extra={"lead_id": record.lead_id, "worksheet": self._worksheet},
        )

    def _append_row(self, service: Any, row: list[str]) -> None:
        """Blocking append - runs in a worker thread."""
        service.spreadsheets().values().append(
            spreadsheetId=self._spreadsheet_id,
            range=f"{self._worksheet}!A:A",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

    # ------------------------------------------------------------------ #
    async def _ensure_header(self, service: Any) -> None:
        """Write the header row once, if the sheet is empty.

        Checked a single time per process: an extra read on every lead would
        double the API calls for no benefit.
        """
        if self._header_checked:
            return
        try:
            existing = await asyncio.to_thread(self._read_first_row, service)
            if not existing:
                await asyncio.to_thread(self._write_header, service)
                logger.info("Header row written to Google Sheet")
        except Exception as exc:
            logger.warning("Could not verify sheet header", extra={"error": str(exc)})
        finally:
            self._header_checked = True

    def _read_first_row(self, service: Any) -> list[Any]:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=f"{self._worksheet}!A1:H1")
            .execute()
        )
        return list(result.get("values", []))

    def _write_header(self, service: Any) -> None:
        service.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet_id,
            range=f"{self._worksheet}!A1",
            valueInputOption="RAW",
            body={"values": [list(LEAD_COLUMNS)]},
        ).execute()

    # ------------------------------------------------------------------ #
    async def health_check(self) -> bool:
        if not self.enabled:
            return False
        try:
            await self._ensure_service()
        except Exception:  # pragma: no cover - configuration probe
            return False
        return True
