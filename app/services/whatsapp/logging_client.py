"""A messaging client that logs instead of sending.

Used when `WHATSAPP_ENABLED=false`, so the whole conversation flow can be driven
locally through the `/api/v1/simulate` endpoint without WhatsApp credentials.
"""

from __future__ import annotations

import uuid

from app.core.logging import get_logger
from app.domain.messaging import OutboundMessage

logger = get_logger(__name__)


class LoggingMessagingClient:
    def __init__(self) -> None:
        #: Everything "sent", newest last - handy in tests and local debugging.
        self.sent: list[tuple[str, OutboundMessage]] = []
        #: Canned attachment bytes, keyed by media id. Nothing sets this in
        #: normal use; tests populate it to exercise the download path without a
        #: Cloud API behind them.
        self.media: dict[str, tuple[bytes, str]] = {}

    async def send(self, to: str, message: OutboundMessage) -> str | None:
        self.sent.append((to, message))
        options = ", ".join(title for _, title in message.options)
        logger.info(
            "[whatsapp disabled] would send: %s%s",
            message.rendered_text(),
            f"  |  options: {options}" if options else "",
            extra={"to": to},
        )
        return f"local-{uuid.uuid4().hex[:12]}"

    async def mark_read(self, wa_message_id: str, *, typing: bool = False) -> None:
        return None

    async def download_media(self, media_id: str) -> tuple[bytes, str] | None:
        """Whatever was stubbed in; nothing at all by default."""
        return self.media.get(media_id)

    async def close(self) -> None:
        return None
