"""Inbound webhook parsing and signature verification."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.domain.enums import MessageKind
from app.domain.messaging import InboundMessage
from app.schemas.whatsapp import WebhookPayload, WhatsAppMessage

logger = get_logger(__name__)


def verify_signature(payload: bytes, header: str | None, app_secret: str) -> bool:
    """Validate Meta's `X-Hub-Signature-256` header.

    Without this the webhook is an open endpoint that anyone who learns the URL
    can post fabricated messages to. Compared in constant time.
    """
    if not app_secret:
        # Nothing to verify against; the caller decides whether that is allowed.
        return False
    if not header or not header.startswith("sha256="):
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"), msg=payload, digestmod=hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: WebhookPayload) -> list[InboundMessage]:
    """Extract the actionable user messages from a webhook payload.

    Delivery receipts, read receipts and unknown event shapes yield an empty
    list - they are normal traffic, not errors.
    """
    messages: list[InboundMessage] = []

    for entry in payload.entry:
        for change in entry.changes:
            value = change.value
            if value is None or not value.messages:
                continue

            # `contacts` carries the WhatsApp profile name, keyed by wa_id.
            profile_names = {
                contact.wa_id: contact.profile.name
                for contact in value.contacts
                if contact.wa_id and contact.profile and contact.profile.name
            }

            for raw in value.messages:
                parsed = _parse_message(raw, profile_names)
                if parsed is not None:
                    messages.append(parsed)

    return messages


def _parse_message(
    raw: WhatsAppMessage, profile_names: dict[str, str]
) -> InboundMessage | None:
    if not raw.id or not raw.from_:
        return None

    kind = MessageKind.UNSUPPORTED
    text = ""
    reply_id: str | None = None
    media_type: str | None = None
    media_id: str | None = None
    media_mime: str | None = None

    match raw.type:
        case "text":
            kind = MessageKind.TEXT
            text = (raw.text.body if raw.text and raw.text.body else "").strip()

        case "interactive":
            kind = MessageKind.INTERACTIVE
            interactive = raw.interactive
            reply = None
            if interactive is not None:
                reply = interactive.button_reply or interactive.list_reply
            if reply is not None:
                reply_id = reply.id
                # Carry the visible title too, so handlers can fall back to text
                # matching if an id ever goes stale after a menu change.
                text = (reply.title or "").strip()

        case "button":
            # Quick-reply on a template message: the payload is our own id.
            kind = MessageKind.BUTTON
            if raw.button is not None:
                reply_id = raw.button.payload
                text = (raw.button.text or "").strip()

        case _:
            # Not read, but the type is kept: an image sent after a payment link
            # is proof of payment, and a voice note is not. Without this both
            # arrive as an indistinguishable UNSUPPORTED.
            media_type = raw.type
            # The id is the only handle Cloud API gives us for the bytes - there
            # is no URL in the webhook and no WhatsApp app to open it in, so
            # without this the picture is unreachable forever.
            attachment = raw.image or raw.document
            if attachment is not None:
                media_id = attachment.id
                media_mime = attachment.mime_type
                if attachment.caption:
                    text = attachment.caption.strip()
            logger.info(
                "Ignoring unsupported message type",
                extra={"type": raw.type, "wa_message_id": raw.id},
            )

    timestamp: datetime | None = None
    if raw.timestamp and raw.timestamp.isdigit():
        timestamp = datetime.fromtimestamp(int(raw.timestamp), tz=UTC)

    return InboundMessage(
        wa_message_id=raw.id,
        from_phone=raw.from_,
        kind=kind,
        text=text,
        reply_id=reply_id,
        profile_name=profile_names.get(raw.from_),
        timestamp=timestamp,
        media_type=media_type,
        media_id=media_id,
        media_mime=media_mime,
    )
