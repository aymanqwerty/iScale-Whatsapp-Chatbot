"""Authenticating to Google as the runtime's own identity.

Downloaded service-account keys are a liability: one arrived here a single
base64 character short, loaded cleanly, reported healthy, and then failed on
its first real write. Newer GCP projects block key downloads outright by org
policy, so on Cloud Run the attached service account is both the safer and the
only route.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import CRMError
from app.services.crm.google_sheets import GoogleSheetsLeadSink
from tests.conftest import PROJECT_ROOT


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "knowledge_dir": PROJECT_ROOT / "knowledge",
        "google_sheets_enabled": True,
        "google_sheets_spreadsheet_id": "sheet-123",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_adc_needs_no_key_to_be_enabled() -> None:
    """The whole point: no JSON, no file, still usable."""
    sink = GoogleSheetsLeadSink(_settings(google_use_adc=True))

    assert sink.enabled is True


def test_without_adc_a_key_is_still_required() -> None:
    """Local development keeps the explicit-key behaviour."""
    sink = GoogleSheetsLeadSink(_settings(google_use_adc=False))

    assert sink.enabled is False


def test_adc_is_preferred_over_a_configured_key() -> None:
    """If both are set, the runtime identity wins - it cannot be mistyped."""
    calls: list[str] = []

    settings = _settings(
        google_use_adc=True,
        google_service_account_json='{"type":"service_account"}',
    )
    sink = GoogleSheetsLeadSink(settings)

    import google.auth

    original = google.auth.default

    def fake_default(scopes=None):  # type: ignore[no-untyped-def]
        calls.append("adc")
        raise RuntimeError("no credentials here")

    google.auth.default = fake_default  # type: ignore[assignment]
    try:
        with pytest.raises(CRMError, match="GOOGLE_USE_ADC"):
            sink._build_service()
    finally:
        google.auth.default = original  # type: ignore[assignment]

    assert calls == ["adc"], "it fell back to the key instead of using ADC"


def test_a_missing_adc_credential_is_a_clear_error() -> None:
    """Not a stack trace about NoneType - it must name the setting."""
    sink = GoogleSheetsLeadSink(_settings(google_use_adc=True))

    import google.auth

    original = google.auth.default

    def fake_default(scopes=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("could not automatically determine credentials")

    google.auth.default = fake_default  # type: ignore[assignment]
    try:
        with pytest.raises(CRMError) as err:
            sink._build_service()
    finally:
        google.auth.default = original  # type: ignore[assignment]

    assert "GOOGLE_USE_ADC" in str(err.value)


def test_a_corrupt_key_still_fails_loudly_without_adc(tmp_path: Path) -> None:
    """The failure that started this: a key one base64 character short."""
    bad = tmp_path / "sa.json"
    bad.write_text('{"type":"service_account","private_key":"not base64 at all"}')
    sink = GoogleSheetsLeadSink(
        _settings(google_use_adc=False, google_service_account_file=str(bad))
    )

    assert sink.enabled is True  # it parses as JSON, so it looks fine
    with pytest.raises(Exception):  # ...and only breaks when actually used
        sink._build_service()
