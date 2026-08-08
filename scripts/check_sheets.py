"""Verify the Google Sheets setup and say exactly what is still missing.

Run it any time:

    .venv\\Scripts\\python.exe scripts\\check_sheets.py

It checks each requirement in order and stops at the first failure with the
specific fix, rather than leaving you to decode a 403 from the Sheets API.
Read-only apart from an optional --write-test.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402

OK = "  [OK]  "
NO = "  [--]  "


def fail(message: str, *fix: str) -> int:
    print(f"{NO}{message}")
    if fix:
        print("\n  WHAT TO DO:")
        for line in fix:
            print(f"    {line}")
    return 1


def main() -> int:
    settings = get_settings()
    print("Google Sheets setup check\n" + "=" * 60)

    # 1. Feature flag ------------------------------------------------------
    if not settings.google_sheets_enabled:
        return fail(
            "GOOGLE_SHEETS_ENABLED is false",
            "Set GOOGLE_SHEETS_ENABLED=true in .env",
        )
    print(f"{OK}GOOGLE_SHEETS_ENABLED is true")

    # 2. Spreadsheet id ----------------------------------------------------
    if not settings.google_sheets_spreadsheet_id:
        return fail(
            "GOOGLE_SHEETS_SPREADSHEET_ID is empty",
            "Copy the id from the sheet URL, between /d/ and /edit",
        )
    print(f"{OK}Spreadsheet id: {settings.google_sheets_spreadsheet_id}")
    print(f"{OK}Worksheet tab the bot writes to: {settings.google_sheets_worksheet_name!r}")

    # 3. Credentials file --------------------------------------------------
    info = settings.google_credentials_info()
    if info is None:
        expected = settings.google_service_account_file or "(unset)"
        path = (ROOT / expected).resolve() if not Path(expected).is_absolute() else expected
        return fail(
            f"No service-account credentials found at {path}",
            "In Google Cloud Console: create a service account, then",
            "Keys -> Add Key -> Create new key -> JSON, and save the",
            f"downloaded file to exactly: {path}",
        )
    email = info.get("client_email", "(missing client_email)")
    print(f"{OK}Service-account key loaded")
    print(f"        client_email: {email}")

    # 4. Library present ---------------------------------------------------
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        return fail(
            "google-api-python-client / google-auth are not installed",
            "Run: .venv\\Scripts\\python.exe -m pip install -r requirements.txt",
        )
    print(f"{OK}Google client libraries installed")

    # 5. Can we actually open the spreadsheet? -----------------------------
    try:
        credentials = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        meta = (
            service.spreadsheets()
            .get(spreadsheetId=settings.google_sheets_spreadsheet_id)
            .execute()
        )
    except Exception as exc:  # the message is the whole point of this script
        text = str(exc)
        if "403" in text or "permission" in text.lower():
            return fail(
                "The service account cannot open the spreadsheet (403)",
                "This is the step people miss. The service account is its own",
                "Google identity - the sheet must be shared with it:",
                "",
                "  1. Open the spreadsheet in your browser",
                "  2. Click Share",
                f"  3. Paste this address: {email}",
                "  4. Set the role to Editor",
                "  5. Untick 'Notify people', then Share",
            )
        if "404" in text:
            return fail(
                "Spreadsheet not found (404)",
                "GOOGLE_SHEETS_SPREADSHEET_ID does not match a real sheet.",
                "Check the id in .env against the sheet URL.",
            )
        if "has not been used" in text or "disabled" in text.lower():
            return fail(
                "The Google Sheets API is not enabled for this project",
                "Google Cloud Console -> APIs & Services -> Library ->",
                "search 'Google Sheets API' -> Enable",
            )
        return fail(f"Could not open the spreadsheet: {text[:300]}")

    title = meta.get("properties", {}).get("title", "(untitled)")
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
    print(f"{OK}Opened spreadsheet: {title!r}")
    print(f"{OK}Tabs present: {tabs}")

    # 6. Does the target tab exist? ---------------------------------------
    target = settings.google_sheets_worksheet_name
    if target not in tabs:
        return fail(
            f"No tab named {target!r} in this spreadsheet",
            f"Either rename a tab to {target!r} (double-click the tab at the",
            "bottom of the sheet), or change GOOGLE_SHEETS_WORKSHEET_NAME in",
            f".env to one of: {tabs}",
        )
    print(f"{OK}Target tab {target!r} exists")

    print("\n" + "=" * 60)
    print("  Everything is configured. Restart the app, then backfill with:")
    print("    curl -X POST http://localhost:8000/api/v1/leads/sync-pending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
