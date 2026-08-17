"""Google Sheets lead sink.

The Google API client is synchronous, so every call is pushed to a worker
thread with `asyncio.to_thread` - blocking the event loop inside a webhook
handler would stall every other conversation in flight.

Setup: create a service account, download the JSON key, and share the target
spreadsheet with the service account's email address as an Editor.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from app.core.config import Settings
from app.core.exceptions import CRMError
from app.core.logging import get_logger
from app.services.crm.base import LeadRecord

logger = get_logger(__name__)

_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


class GoogleSheetsLeadSink:
    """Appends one row per lead to a worksheet."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._spreadsheet_id = settings.google_sheets_spreadsheet_id
        self._service: Any | None = None
        self._lock = asyncio.Lock()
        # Tracked per tab: the two tabs carry different headers, so one shared
        # "already checked" flag would leave the second tab unheadered.
        self._headers_checked: set[str] = set()
        self._formatted: set[str] = set()
        self._known_tabs: set[str] = set()

    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return "google_sheets"

    @property
    def enabled(self) -> bool:
        return bool(
            self._settings.google_sheets_enabled
            and self._spreadsheet_id
            and (
                self._settings.google_use_adc
                or self._settings.google_credentials_info()
            )
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

        if self._settings.google_use_adc:
            # The runtime's own identity - on Cloud Run, the attached service
            # account. Nothing to store and nothing to expire, so the sheet
            # cannot break because a key was copied badly.
            try:
                import google.auth
            except ImportError as exc:  # pragma: no cover - dependency missing
                raise CRMError("google-auth is not installed") from exc
            try:
                credentials, _ = google.auth.default(scopes=list(_SCOPES))
            except Exception as exc:
                raise CRMError(
                    f"GOOGLE_USE_ADC is on but no default credentials were found: {exc}"
                ) from exc
        else:
            info = self._settings.google_credentials_info()
            if info is None:
                raise CRMError("No Google service-account credentials configured")
            credentials = Credentials.from_service_account_info(
                info, scopes=list(_SCOPES)
            )
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
    def worksheet_for(self, record: LeadRecord) -> str:
        """Which tab this lead belongs on.

        Routing by lead type rather than by anything the model produced: the
        funnel side is decided by the state machine, so a lead cannot land on
        the wrong team's tab because a sentence was ambiguous.
        """
        return (
            self._settings.google_sheets_post_sales_worksheet
            if record.is_post_sales
            else self._settings.google_sheets_pre_sales_worksheet
        )

    async def push_lead(self, record: LeadRecord) -> None:
        if not self.enabled:
            raise CRMError("Google Sheets sink is not enabled or not configured")

        service = await self._ensure_service()
        worksheet = self.worksheet_for(record)
        columns = record.columns

        await self._ensure_worksheet(service, worksheet)
        await self._ensure_header(service, worksheet, columns)

        try:
            await asyncio.to_thread(
                self._append_row, service, worksheet, record.as_row(columns)
            )
        except CRMError:
            raise
        except Exception as exc:
            logger.exception("Failed to append lead to Google Sheets")
            raise CRMError(f"Google Sheets append failed: {exc}") from exc

        logger.info(
            "Lead appended to Google Sheets",
            extra={"lead_id": record.lead_id, "worksheet": worksheet},
        )

        # Formatting has to come after the append, not with the header:
        # `INSERT_ROWS` creates brand new rows, and a format applied to empty
        # space before they existed does not reach them - the counselor would
        # see "46242.69" where a date should be. Once per tab per process is
        # enough, because rows inserted after a formatted row inherit from it.
        if worksheet not in self._formatted:
            self._formatted.add(worksheet)
            await asyncio.to_thread(
                self._apply_date_formats, service, worksheet, columns
            )

    async def _ensure_worksheet(self, service: Any, worksheet: str) -> None:
        """Create the tab if it is not there.

        The two tabs are new, and nobody has made them by hand. Without this the
        very first lead of each kind would fail with "Unable to parse range" -
        the one moment the sheet mattered most.
        """
        if worksheet in self._known_tabs:
            return
        try:
            existing = await asyncio.to_thread(self._worksheet_titles, service)
            if worksheet not in existing:
                await asyncio.to_thread(self._create_worksheet, service, worksheet)
                logger.info("Created worksheet", extra={"worksheet": worksheet})
            self._known_tabs.add(worksheet)
        except Exception as exc:
            # Not fatal: the append below may still work if the tab exists and
            # only the metadata read failed. A lost lead is the worse outcome.
            logger.warning(
                "Could not verify the worksheet exists",
                extra={"worksheet": worksheet, "error": str(exc)},
            )

    def _worksheet_titles(self, service: Any) -> set[str]:
        meta = service.spreadsheets().get(spreadsheetId=self._spreadsheet_id).execute()
        return {
            str(sheet.get("properties", {}).get("title", ""))
            for sheet in meta.get("sheets", [])
        }

    def _create_worksheet(self, service: Any, worksheet: str) -> None:
        service.spreadsheets().batchUpdate(
            spreadsheetId=self._spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": worksheet}}}]},
        ).execute()

    def _append_row(self, service: Any, worksheet: str, row: list[str]) -> None:
        """Upsert by lead id - update the row if present, else append.

        A rescheduled call must not leave its old time sitting in the sheet: the
        counselor would see two rows and have to guess which is live. Matching
        on the Lead ID column keeps one row per lead.
        """
        lead_id = row[0] if row else ""
        existing = (
            self._find_row(service, worksheet, lead_id) if lead_id else None
        )

        if existing is not None:
            service.spreadsheets().values().update(
                spreadsheetId=self._spreadsheet_id,
                range=f"{worksheet}!A{existing}",
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            ).execute()
            logger.info("Updated existing sheet row", extra={"row": existing})
            return

        service.spreadsheets().values().append(
            spreadsheetId=self._spreadsheet_id,
            range=f"{worksheet}!A:A",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

    def _find_row(self, service: Any, worksheet: str, lead_id: str) -> int | None:
        """1-based sheet row for this lead, or None if it is not there yet.

        Reads only column A. Best-effort: on any failure we fall back to
        appending, because a duplicate row is a far better outcome than a lost
        lead.
        """
        try:
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=self._spreadsheet_id, range=f"{worksheet}!A:A")
                .execute()
            )
        except Exception as exc:
            logger.warning("Could not scan the sheet for an existing row",
                           extra={"error": str(exc)})
            return None

        for index, cells in enumerate(result.get("values", []), start=1):
            if cells and str(cells[0]).strip() == lead_id:
                return index
        return None

    # ------------------------------------------------------------------ #
    async def _ensure_header(
        self, service: Any, worksheet: str, columns: tuple[str, ...]
    ) -> None:
        """Write or correct the header row.

        Checked a single time per process: an extra read on every lead would
        double the API calls for no benefit.

        A stale header is repaired, not just a missing one. When a tab's columns
        gains a column, an existing sheet would otherwise keep the old headings
        while new rows carry more values - so the data silently shifts under
        labels that no longer describe it.
        """
        if worksheet in self._headers_checked:
            return
        try:
            existing = await asyncio.to_thread(
                self._read_first_row, service, worksheet, columns
            )
            current = list(existing[0]) if existing else []
            if not current:
                await asyncio.to_thread(self._write_header, service, worksheet, columns)
                logger.info(
                    "Header row written", extra={"worksheet": worksheet}
                )
            elif current != list(columns):
                await asyncio.to_thread(self._write_header, service, worksheet, columns)
                logger.warning(
                    "Google Sheet header was out of date and has been rewritten. "
                    "Rows written before this point may not line up with the new "
                    "columns - re-sync them with POST /leads/sync-pending after "
                    "clearing the sheet.",
                    extra={"was": current, "now": list(columns), "worksheet": worksheet},
                )
        except Exception as exc:
            logger.warning("Could not verify sheet header", extra={"error": str(exc)})
        finally:
            self._headers_checked.add(worksheet)

    @staticmethod
    def _last_column(columns: tuple[str, ...]) -> str:
        """Column letter for the final header, e.g. 10 columns -> "J"."""
        return chr(ord("A") + len(columns) - 1)

    def _read_first_row(
        self, service: Any, worksheet: str, columns: tuple[str, ...]
    ) -> list[Any]:
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{worksheet}!A1:{self._last_column(columns)}1",
            )
            .execute()
        )
        return list(result.get("values", []))

    def _write_header(
        self, service: Any, worksheet: str, columns: tuple[str, ...]
    ) -> None:
        service.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet_id,
            range=f"{worksheet}!A1",
            valueInputOption="RAW",
            body={"values": [list(columns)]},
        ).execute()

    #: Column heading -> the number format its cells should carry. Sheets stores
    #: these as numbers (a date is a serial, a time a fraction of a day), so
    #: without a format the counselor sees "46242.69" instead of a date.
    _DATE_FORMATS: ClassVar[dict[str, tuple[str, str]]] = {
        "Date": ("DATE_TIME", "yyyy-mm-dd hh:mm"),
        "Callback Date": ("DATE", "ddd, dd mmm yyyy"),
        "Callback Time": ("TIME", "hh:mm am/pm"),
    }

    def _apply_date_formats(
        self, service: Any, worksheet: str, columns: tuple[str, ...]
    ) -> None:
        """Format the date columns so they read as dates, not serial numbers.

        Best-effort: the values are already real dates, so filtering and sorting
        work whether or not this succeeds. Only the display is at stake, and a
        formatting failure must never cost a lead.
        """
        try:
            sheet_id = self._worksheet_id(service, worksheet)
            if sheet_id is None:
                return
            requests = [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,  # leave the header alone
                            "startColumnIndex": columns.index(heading),
                            "endColumnIndex": columns.index(heading) + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": kind, "pattern": pattern}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
                for heading, (kind, pattern) in self._DATE_FORMATS.items()
                if heading in columns
            ]
            service.spreadsheets().batchUpdate(
                spreadsheetId=self._spreadsheet_id, body={"requests": requests}
            ).execute()
            logger.info("Date column formats applied", extra={"worksheet": worksheet})
        except Exception as exc:
            logger.warning(
                "Could not format the date columns; values are still real dates",
                extra={"error": str(exc)},
            )

    def _worksheet_id(self, service: Any, worksheet: str) -> int | None:
        meta = service.spreadsheets().get(spreadsheetId=self._spreadsheet_id).execute()
        for sheet in meta.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == worksheet:
                return int(properties["sheetId"])
        return None

    # ------------------------------------------------------------------ #
    async def health_check(self) -> bool:
        if not self.enabled:
            return False
        try:
            await self._ensure_service()
        except Exception:  # pragma: no cover - configuration probe
            return False
        return True
