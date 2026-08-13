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

#: Vite's dev server, on both hostnames a browser might use. Allowed outside
#: production so `npm run dev` against a deployed backend just works.
_LOCAL_CONSOLE_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

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

    # --- LLM provider ------------------------------------------------------
    #: Which model backend answers questions. Both clients implement the same
    #: `LLMClient` protocol, so switching is an environment variable and a
    #: restart - no redeploy of different code, and no way for the two to drift
    #: apart in behaviour.
    llm_provider: Literal["gemini", "groq"] = "gemini"

    # --- Gemini (LLM) ------------------------------------------------------
    gemini_api_key: SecretStr = SecretStr("")
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    #: Not 2.5-flash. Google retired that family for projects created after the
    #: cutoff: a new key authenticates fine, lists the model, then fails the
    #: actual call with "no longer available to new users" - which reads like a
    #: broken key and is not. Flash-Lite also costs a fraction of 3.5-flash
    #: ($0.25/$1.50 against $1.50/$9.00 per 1M) for answers this bot grounds in
    #: the knowledge base anyway, and it still honours `thinkingBudget`.
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_temperature: float = 0.3
    gemini_max_output_tokens: int = 512
    gemini_timeout_seconds: float = 20.0
    #: Thinking tokens are billed as output and are ON by default. Measured on a
    #: one-line reply: 174 of 206 tokens were thinking - four times the quota for
    #: an answer already grounded by the knowledge section. 0 disables it;
    #: -1 leaves Google's default in place.
    gemini_thinking_budget: int = 0

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

    # --- API protection ----------------------------------------------------
    # The lead endpoints return names, phone numbers and remarks. Set this and
    # callers must send it as `X-API-Key`. Leaving it empty is refused outright
    # in production - the same posture as the WhatsApp app secret, because the
    # failure mode is identical: a quiet warning nobody reads, and personal data
    # served to anyone who can reach the port.
    api_key: SecretStr = SecretStr("")

    # --- Agent console -----------------------------------------------------
    # The internal handover console. It exposes every customer conversation and
    # phone number the bot has spoken to, so it is off unless explicitly turned
    # on and fully configured. A missing hash or secret disables it rather than
    # falling back to something permissive.
    #
    # Generate the two secrets with:
    #   python -m scripts.console_password
    console_enabled: bool = False
    console_username: str = "iScale-user"
    #: `scrypt$<salt>$<hash>`, never a plain password. Deliberately NOT the
    #: database password - anyone who learns this must not thereby gain direct
    #: access to the leads table.
    console_password_hash: SecretStr = SecretStr("")
    #: Signs the session token. Rotating it logs everyone out immediately,
    #: which is the fastest revocation available.
    console_session_secret: SecretStr = SecretStr("")
    #: Origins allowed to call the console API with credentials, comma
    #: separated - e.g. "https://console.theiscale.com,http://localhost:5173".
    #:
    #: Only needed when the frontend is hosted somewhere other than this app.
    #: Deliberately an explicit list and never "*": these endpoints return every
    #: customer transcript, and a wildcard with credentials is both refused by
    #: browsers and wrong in principle.
    console_allowed_origins: str = ""
    #: Optional regex for origins that change on every deploy. Vercel names each
    #: deployment `project-<hash>-team.vercel.app`, so an exact URL works once
    #: and breaks on the next push. Example:
    #:   ^https://i-scale-whatsapp-chatbot-[a-z0-9-]+\.vercel\.app$
    #: Anchor it to your project prefix; a bare `.*\.vercel\.app` would let any
    #: Vercel project in the world call this API with credentials.
    console_allowed_origin_regex: str = ""

    #: Refuse to act on a message WhatsApp says was sent longer ago than this.
    #:
    #: Meta retries a webhook it could not deliver, with backoff, for hours. A
    #: free instance that was asleep therefore wakes to a queue of stale
    #: messages and answers them - which reaches the customer as the bot
    #: messaging them out of nowhere, hours after they last said anything.
    #: Observed: a reply sent at 09:56 to a message from the previous night.
    #:
    #: Generous by default. Real delivery is a second or two, so this only ever
    #: catches genuine redeliveries. Set to 0 to disable the check.
    webhook_max_message_age_seconds: int = 900

    # --- Inactivity follow-up ----------------------------------------------
    #: One check-in when a conversation goes quiet mid-flow, then the chat is
    #: closed so the next message starts fresh. Never sent to someone who
    #: finished (a booking completed, or they said goodbye), to a conversation a
    #: human has taken over, or twice to anyone.
    inactivity_enabled: bool = True
    #: Silence before the first nudge. An hour is long enough that the person is
    #: genuinely away rather than reading, comparing courses in another tab, or
    #: answering a call.
    inactivity_minutes: int = 60
    #: Gap between the first nudge and the second, measured from when the first
    #: was sent. Two attempts total, then silence - a third would be pestering.
    #:
    #: 60 + 360 puts the last message seven hours after they went quiet, safely
    #: inside WhatsApp's 24-hour window. Raising these much risks the second
    #: nudge being refused by Meta rather than merely late.
    inactivity_followup_minutes: int = 360
    #: How often the sweeper looks. A minute is far finer than the 15-minute
    #: threshold needs, and costs one indexed query.
    inactivity_sweep_seconds: int = 60
    #: WhatsApp refuses free-text outside 24 hours of the customer's last
    #: message, so anyone quieter than this is unreachable and is skipped rather
    #: than attempted and logged as a failure.
    inactivity_max_age_hours: int = 23

    rate_limit_enabled: bool = True
    #: Messages one sender may have processed per window. Each one costs an LLM
    #: call, so this is a spend control as much as an abuse control.
    rate_limit_per_sender: int = 20
    rate_limit_window_seconds: float = 60.0
    #: Raw webhook requests accepted per source address per window. Generous,
    #: because Meta legitimately bursts on redelivery.
    rate_limit_webhook_per_ip: int = 120

    # --- Google Sheets -----------------------------------------------------
    google_sheets_enabled: bool = False
    google_sheets_spreadsheet_id: str = ""
    #: Legacy single-tab name. Kept so an existing deployment does not lose its
    #: sheet on upgrade; new writes are routed to the two tabs below.
    google_sheets_worksheet_name: str = "Leads"
    #: Leads are split by funnel side. The two sides carry different columns and
    #: are worked by different teams, so one combined tab meant every counselor
    #: scrolled past columns that were permanently blank for their half.
    #: Created automatically if absent.
    google_sheets_pre_sales_worksheet: str = "Pre Sales"
    google_sheets_post_sales_worksheet: str = "Post Sales"
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

    @property
    def console_origins(self) -> list[str]:
        """Parsed `console_allowed_origins`, empty when same-origin only.

        A bare host is upgraded to `https://host`. Browsers always send the
        scheme in the `Origin` header, so "example.vercel.app" matches nothing -
        and the failure is completely silent: the request never reaches the
        server, so the logs show nothing at all and the console simply cannot
        log in. Accepting the shorter form costs nothing and removes an entire
        class of afternoon.
        """
        origins = list(self.configured_console_origins)

        # The Vite dev server, allowed automatically outside production. Running
        # the console locally against a deployed backend is the normal way to
        # work on it, and requiring an env var for that means every developer
        # meets an opaque CORS error before they can start. Never added in
        # production, where the frontend is served from a real domain.
        #
        # Deliberately kept OUT of `console_cross_origin`: this is a CORS
        # convenience, and letting it also relax the session cookie to
        # SameSite=None would weaken every local and test deployment for the
        # sake of a dev server.
        if not self.is_production:
            for dev in _LOCAL_CONSOLE_ORIGINS:
                if dev not in origins:
                    origins.append(dev)
        return origins

    @property
    def configured_console_origins(self) -> list[str]:
        """Only what the operator actually set, normalised."""
        origins: list[str] = []
        for raw in self.console_allowed_origins.split(","):
            origin = raw.strip().rstrip("/")
            if not origin:
                continue
            if "://" not in origin:
                # localhost is http in practice; anything deployed is https.
                scheme = (
                    "http"
                    if origin.startswith(("localhost", "127.0.0.1"))
                    else "https"
                )
                origin = f"{scheme}://{origin}"
            origins.append(origin)
        return origins

    @property
    def console_origin_regex(self) -> str | None:
        """Pattern matching Vercel preview deployments, when one is configured.

        Vercel gives every deployment its own hostname
        (`project-<hash>-team.vercel.app`), so pinning the exact URL works until
        the next push and then breaks. Setting the production domain here lets
        its previews through as well, matched on the project prefix rather than
        `.vercel.app` at large - which would admit anybody's Vercel project.
        """
        pattern = self.console_allowed_origin_regex.strip()
        return pattern or None

    @property
    def console_cross_origin(self) -> bool:
        """Whether a separately hosted frontend is expected.

        Drives the cookie's SameSite policy: a cookie stays `lax` (the safer
        default) until an external origin is actually configured, so a
        same-origin deployment is never loosened for a frontend that does not
        exist.
        """
        return bool(self.configured_console_origins or self.console_origin_regex)

    @property
    def console_ready(self) -> bool:
        """Whether the console is switched on AND has everything it needs.

        Both secrets are required. A console served with an empty password hash
        would authenticate nobody but still expose the login page, and one with
        an empty signing secret would accept any forged cookie - so a partial
        configuration is treated as off.
        """
        return bool(
            self.console_enabled
            and self.console_username
            and self.console_password_hash.get_secret_value()
            and self.console_session_secret.get_secret_value()
        )

    def google_credentials_info(self) -> dict[str, Any] | None:
        """Service-account credentials as a dict, from inline JSON or a file.

        Returns None rather than raising when the key is unreadable. This is
        called during startup to decide whether the Sheets sink is usable, so a
        malformed key used to crash the whole process in a boot loop - taking
        WhatsApp down over a spreadsheet mirror. Leads live in PostgreSQL; the
        sheet is a convenience, and it must never be able to stop the bot.

        Pasting the key into an environment variable is where this bites: the
        `private_key` field is full of `\\n` escapes and is easily mangled.
        """
        import logging

        if self.google_service_account_json is not None:
            raw = self.google_service_account_json.get_secret_value().strip()
            if raw:
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    logging.getLogger(__name__).error(
                        "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON (%s). "
                        "Sheets sync is disabled; leads are still saved to "
                        "PostgreSQL and can be pushed later with "
                        "POST /api/v1/leads/sync-pending.",
                        exc,
                    )
                    return None
        if self.google_service_account_file:
            path = Path(self.google_service_account_file).expanduser()
            if not path.is_absolute():
                path = (BASE_DIR / path).resolve()
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    logging.getLogger(__name__).error(
                        "Could not read the service-account file at %s (%s). "
                        "Sheets sync is disabled; leads are still saved to "
                        "PostgreSQL.",
                        path,
                        exc,
                    )
                    return None
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that the `.env` file is parsed once. Tests can clear the cache
    with `get_settings.cache_clear()`.
    """
    return Settings()
