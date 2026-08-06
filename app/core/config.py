"""Application configuration.

All runtime configuration lives here and is read from the environment (or a
`.env` file) exactly once. Everything else in the codebase receives settings
through dependency injection rather than importing globals, which keeps the
services testable.
"""

from __future__ import annotations

import json
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR: Path = Path(__file__).resolve().parents[2]

_WEEKDAY_NAMES: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class Settings(BaseSettings):
    """Typed, validated view of the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------
    app_name: str = "iScale WhatsApp Receptionist"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = False
    api_prefix: str = "/api/v1"

    # --- Database ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://iscale:iscale@localhost:5432/iscale"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # --- Groq (LLM) --------------------------------------------------------
    groq_api_key: SecretStr = SecretStr("")
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"
    groq_temperature: float = 0.3
    groq_max_output_tokens: int = 512
    groq_timeout_seconds: float = 20.0

    # --- WhatsApp Cloud API ------------------------------------------------
    whatsapp_enabled: bool = True
    whatsapp_api_version: str = "v21.0"
    whatsapp_base_url: str = "https://graph.facebook.com"
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_timeout_seconds: float = 15.0

    # --- Development safety net --------------------------------------------
    # While developing against a LIVE business number, the webhook receives
    # every message sent to that number - including real customers and traffic
    # belonging to other services on the same WABA. With the allowlist on, any
    # message from a number not listed here is dropped before it is processed,
    # and no reply can be sent to it.
    #
    # Defaults are fail-closed on purpose: enabled, with an empty list, means
    # the bot talks to nobody. Silence during development is a recoverable
    # mistake; messaging a real customer is not.
    whatsapp_allowlist_enabled: bool = True
    whatsapp_allowed_numbers: str = ""

    # --- Google Sheets -----------------------------------------------------
    google_sheets_enabled: bool = False
    google_sheets_spreadsheet_id: str = ""
    google_sheets_worksheet_name: str = "Leads"
    google_service_account_file: str | None = None
    google_service_account_json: SecretStr | None = None

    # --- Business rules ----------------------------------------------------
    business_timezone: str = "Asia/Kolkata"
    business_open_time: time = time(11, 0)
    business_close_time: time = time(19, 0)
    business_closed_weekdays: str = "friday"
    callback_max_days_ahead: int = 30
    callback_min_lead_minutes: int = 30

    # --- Knowledge base ----------------------------------------------------
    knowledge_dir: Path = BASE_DIR / "knowledge"
    knowledge_max_snippets: int = 6
    knowledge_max_chars: int = 6000

    # --- Conversation ------------------------------------------------------
    qna_nudge_threshold: int = 3
    history_message_limit: int = 10

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Unsupported log level: {value}")
        return level

    @field_validator("business_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - config error path
            raise ValueError(f"Unknown timezone: {value}") from exc
        return value

    @field_validator("knowledge_dir", mode="before")
    @classmethod
    def _resolve_knowledge_dir(cls, value: Any) -> Any:
        if value in (None, ""):
            return BASE_DIR / "knowledge"
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else (BASE_DIR / path).resolve()

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            # A very common mistake; fix it rather than failing at connect time.
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    # ------------------------------------------------------------------ #
    # Derived values
    # ------------------------------------------------------------------ #
    @computed_field  # type: ignore[prop-decorator]
    @property
    def tz(self) -> ZoneInfo:
        """Business timezone as a tzinfo object."""
        return ZoneInfo(self.business_timezone)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def closed_weekdays(self) -> frozenset[int]:
        """Weekday numbers the company is closed (Monday = 0)."""
        days: set[int] = set()
        for raw in self.business_closed_weekdays.split(","):
            token = raw.strip().lower()
            if not token:
                continue
            if token.isdigit():
                days.add(int(token) % 7)
            elif token in _WEEKDAY_NAMES:
                days.add(_WEEKDAY_NAMES[token])
            else:
                raise ValueError(f"Unknown weekday: {raw!r}")
        return frozenset(days)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def google_credentials_info(self) -> dict[str, Any] | None:
        """Service-account credentials as a dict, from inline JSON or a file."""
        if self.google_service_account_json is not None:
            raw = self.google_service_account_json.get_secret_value().strip()
            if raw:
                return json.loads(raw)
        if self.google_service_account_file:
            path = Path(self.google_service_account_file).expanduser()
            if not path.is_absolute():
                path = (BASE_DIR / path).resolve()
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that the `.env` file is parsed once. Tests can clear the cache
    with `get_settings.cache_clear()`.
    """
    return Settings()
