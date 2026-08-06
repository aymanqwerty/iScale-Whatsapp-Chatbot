"""WhatsApp Cloud API client.

Owns the entire mapping from our channel-agnostic `OutboundMessage` to Meta's
JSON. Nothing else in the codebase should know what a `messaging_product` field
is.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, WhatsAppError
from app.core.logging import get_logger
from app.domain.messaging import OutboundMessage

logger = get_logger(__name__)

#: HTTP statuses worth retrying: rate limiting and transient upstream failures.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class WhatsAppClient:
    """Sends messages through the WhatsApp Cloud API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._phone_number_id = settings.whatsapp_phone_number_id
        self._token = settings.whatsapp_access_token.get_secret_value()
        self._base_url = (
            f"{settings.whatsapp_base_url.rstrip('/')}/{settings.whatsapp_api_version}"
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=settings.whatsapp_timeout_seconds)

    # ------------------------------------------------------------------ #
    @property
    def _endpoint(self) -> str:
        return f"{self._base_url}/{self._phone_number_id}/messages"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _require_config(self) -> None:
        if not self._phone_number_id or not self._token:
            raise ConfigurationError(
                "WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN must be set"
            )

    # ------------------------------------------------------------------ #
    async def send(self, to: str, message: OutboundMessage) -> str | None:
        self._require_config()
        body = self.build_payload(to, message)
        response = await self._post(body)
        message_id = _first_message_id(response)
        logger.info(
            "WhatsApp message sent",
            extra={"to": _mask(to), "wa_message_id": message_id,
                   "interactive": message.is_interactive},
        )
        return message_id

    async def mark_read(self, wa_message_id: str) -> None:
        """Best effort - a failed read receipt must never break a reply."""
        try:
            self._require_config()
            await self._post(
                {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": wa_message_id,
                }
            )
        except Exception as exc:
            logger.debug("Could not mark message as read", extra={"error": str(exc)})

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Payload construction
    # ------------------------------------------------------------------ #
    def build_payload(self, to: str, message: OutboundMessage) -> dict[str, Any]:
        """Render an `OutboundMessage` as Cloud API JSON."""
        base: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
        }

        if message.buttons:
            interactive: dict[str, Any] = {
                "type": "button",
                "body": {"text": message.rendered_text()},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {"id": button.id, "title": button.rendered_title()},
                        }
                        for button in message.buttons
                    ]
                },
            }
        elif message.list_rows:
            interactive = {
                "type": "list",
                "body": {"text": message.rendered_text()},
                "action": {
                    "button": message.list_button_label[:20],
                    "sections": [
                        {
                            "title": (message.header or "Options")[:24],
                            "rows": [
                                _row_payload(row) for row in message.list_rows
                            ],
                        }
                    ],
                },
            }
        else:
            return {**base, "type": "text",
                    "text": {"preview_url": False, "body": message.rendered_text()}}

        if message.header:
            interactive["header"] = {"type": "text", "text": message.header[:60]}
        if message.footer:
            interactive["footer"] = {"text": message.footer[:60]}

        return {**base, "type": "interactive", "interactive": interactive}

    # ------------------------------------------------------------------ #
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(WhatsAppError),
        reraise=True,
    )
    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._endpoint, json=body, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"HTTP error calling WhatsApp: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUS:
            raise WhatsAppError(
                f"WhatsApp returned {response.status_code}",
                status_code=response.status_code,
                body=response.text[:500],
            )

        if response.is_error:
            # 4xx other than rate limiting means a bad request - retrying will
            # not help, so surface it immediately with Meta's own error detail.
            detail = _error_detail(response)
            logger.error(
                "WhatsApp rejected the message",
                extra={"status": response.status_code, "detail": detail},
            )
            raise WhatsAppError(f"WhatsApp rejected the request: {detail}")

        try:
            return dict(response.json())
        except ValueError:
            return {}


def _row_payload(row: Any) -> dict[str, str]:
    payload = {"id": row.id, "title": row.rendered_title()}
    description = row.rendered_description()
    if description:
        payload["description"] = description
    return payload


def _first_message_id(response: dict[str, Any]) -> str | None:
    messages = response.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            value = first.get("id")
            return str(value) if value else None
    return None


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return f"{error.get('code')}: {error.get('message')}"
    return str(payload)[:300]


def _mask(phone: str) -> str:
    """Keep phone numbers out of logs in readable-but-not-identifying form."""
    return f"{phone[:4]}***{phone[-2:]}" if len(phone) > 6 else "***"
