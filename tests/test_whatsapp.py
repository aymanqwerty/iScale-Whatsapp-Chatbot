"""Webhook parsing, signature verification and outbound payload rendering."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.core.config import Settings
from app.domain.enums import MessageKind
from app.domain.messaging import Button, ListRow, OutboundMessage
from app.schemas.whatsapp import WebhookPayload
from app.services.whatsapp.allowlist import PhoneAllowlist
from app.services.whatsapp.client import WhatsAppClient
from app.services.whatsapp.guarded_client import GuardedMessagingClient
from app.services.whatsapp.logging_client import LoggingMessagingClient
from app.services.whatsapp.parser import parse_webhook, verify_signature

APP_SECRET = "super-secret"


def _payload(message: dict[str, object]) -> dict[str, object]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "999"},
                            "contacts": [
                                {"wa_id": "919876543210", "profile": {"name": "Rahul"}}
                            ],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Signature verification
# --------------------------------------------------------------------------- #
def test_valid_signature_passes() -> None:
    body = b'{"hello":"world"}'
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert verify_signature(body, f"sha256={digest}", APP_SECRET)


def test_tampered_body_fails() -> None:
    body = b'{"hello":"world"}'
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert not verify_signature(b'{"hello":"evil"}', f"sha256={digest}", APP_SECRET)


@pytest.mark.parametrize(
    "header", [None, "", "sha1=abc", "sha256=deadbeef", "deadbeef"]
)
def test_malformed_signature_headers_fail(header: str | None) -> None:
    assert not verify_signature(b"{}", header, APP_SECRET)


def test_missing_app_secret_never_passes() -> None:
    """An unconfigured secret must not silently authorise every request."""
    assert not verify_signature(b"{}", "sha256=whatever", "")


# --------------------------------------------------------------------------- #
# Inbound parsing
# --------------------------------------------------------------------------- #
def test_text_message_is_parsed() -> None:
    payload = WebhookPayload.model_validate(
        _payload(
            {
                "id": "wamid.abc",
                "from": "919876543210",
                "timestamp": "1754380800",
                "type": "text",
                "text": {"body": "  Hello there  "},
            }
        )
    )

    messages = parse_webhook(payload)

    assert len(messages) == 1
    message = messages[0]
    assert message.wa_message_id == "wamid.abc"
    assert message.from_phone == "919876543210"
    assert message.text == "Hello there"
    assert message.kind is MessageKind.TEXT
    assert message.profile_name == "Rahul"
    assert message.timestamp is not None


def test_button_reply_carries_the_option_id() -> None:
    payload = WebhookPayload.model_validate(
        _payload(
            {
                "id": "wamid.btn",
                "from": "919876543210",
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "confirm:yes", "title": "Yes, please"},
                },
            }
        )
    )

    message = parse_webhook(payload)[0]

    assert message.reply_id == "confirm:yes"
    assert message.text == "Yes, please"
    assert message.kind is MessageKind.INTERACTIVE


def test_list_reply_carries_the_option_id() -> None:
    payload = WebhookPayload.model_validate(
        _payload(
            {
                "id": "wamid.list",
                "from": "919876543210",
                "type": "interactive",
                "interactive": {
                    "type": "list_reply",
                    "list_reply": {"id": "course:sql", "title": "SQL"},
                },
            }
        )
    )

    message = parse_webhook(payload)[0]

    assert message.reply_id == "course:sql"


def test_media_message_is_marked_unsupported() -> None:
    payload = WebhookPayload.model_validate(
        _payload(
            {
                "id": "wamid.img",
                "from": "919876543210",
                "type": "image",
                "image": {"id": "media-1"},
            }
        )
    )

    message = parse_webhook(payload)[0]

    assert message.kind is MessageKind.UNSUPPORTED
    assert not message.is_actionable


def test_status_only_payload_yields_nothing() -> None:
    """Delivery receipts are normal traffic, not messages and not errors."""
    payload = WebhookPayload.model_validate(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {"statuses": [{"id": "x", "status": "delivered"}]},
                        }
                    ]
                }
            ],
        }
    )

    assert parse_webhook(payload) == []


def test_unknown_payload_shape_is_tolerated() -> None:
    payload = WebhookPayload.model_validate({"object": "something_else", "entry": []})

    assert parse_webhook(payload) == []


# --------------------------------------------------------------------------- #
# Outbound rendering
# --------------------------------------------------------------------------- #
@pytest.fixture
def client() -> WhatsAppClient:
    settings = Settings(
        _env_file=None,
        whatsapp_phone_number_id="999",
        whatsapp_access_token="token",
    )
    return WhatsAppClient(settings)


def test_plain_text_payload(client: WhatsAppClient) -> None:
    body = client.build_payload("919876543210", OutboundMessage(text="Hello"))

    assert body["type"] == "text"
    assert body["text"]["body"] == "Hello"
    assert body["messaging_product"] == "whatsapp"


def test_button_payload(client: WhatsAppClient) -> None:
    message = OutboundMessage(
        text="Shall I book a call?",
        buttons=(Button(id="confirm:yes", title="Yes"), Button(id="confirm:no", title="No")),
    )

    body = client.build_payload("919876543210", message)

    assert body["type"] == "interactive"
    assert body["interactive"]["type"] == "button"
    buttons = body["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["confirm:yes", "confirm:no"]


def test_list_payload(client: WhatsAppClient) -> None:
    message = OutboundMessage(
        text="Pick a course",
        list_rows=(
            ListRow(id="course:sql", title="SQL", description="Query data"),
            ListRow(id="course:python", title="Python"),
        ),
        list_button_label="View courses",
        header="Our courses",
    )

    body = client.build_payload("919876543210", message)

    assert body["interactive"]["type"] == "list"
    rows = body["interactive"]["action"]["sections"][0]["rows"]
    assert rows[0]["description"] == "Query data"
    assert "description" not in rows[1]
    assert body["interactive"]["header"]["text"] == "Our courses"


def test_long_button_titles_are_truncated(client: WhatsAppClient) -> None:
    """WhatsApp rejects titles over 20 characters outright."""
    message = OutboundMessage(
        text="Choose",
        buttons=(Button(id="a", title="An extremely long button title indeed"),),
    )

    body = client.build_payload("919876543210", message)

    assert len(body["interactive"]["action"]["buttons"][0]["reply"]["title"]) <= 20


def test_payload_is_json_serialisable(client: WhatsAppClient) -> None:
    message = OutboundMessage(
        text="Pick", list_rows=(ListRow(id="a", title="A"),)
    )

    json.dumps(client.build_payload("919876543210", message))


def test_buttons_and_rows_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        OutboundMessage(
            text="x", buttons=(Button(id="a", title="A"),),
            list_rows=(ListRow(id="b", title="B"),),
        )


def test_button_count_is_capped() -> None:
    with pytest.raises(ValueError, match="at most 3 buttons"):
        OutboundMessage(
            text="x",
            buttons=tuple(Button(id=str(i), title=str(i)) for i in range(4)),
        )


# --------------------------------------------------------------------------- #
# Development allowlist
# --------------------------------------------------------------------------- #
def test_allowlist_permits_a_listed_number() -> None:
    guard = PhoneAllowlist(enabled=True, numbers="919876543210")

    assert guard.allows("919876543210")


def test_allowlist_refuses_everyone_else() -> None:
    guard = PhoneAllowlist(enabled=True, numbers="919876543210")

    assert not guard.allows("918888800000")
    assert not guard.allows("")


@pytest.mark.parametrize(
    "listed, inbound",
    [
        ("+91 98765-43210", "919876543210"),  # punctuation and spaces
        ("9876543210", "919876543210"),  # listed without the country code
        ("919876543210", "9876543210"),  # inbound without the country code
    ],
)
def test_allowlist_survives_formatting_differences(listed: str, inbound: str) -> None:
    """A formatting mismatch must not silently switch the guard off."""
    assert PhoneAllowlist(enabled=True, numbers=listed).allows(inbound)


def test_allowlist_accepts_several_numbers() -> None:
    guard = PhoneAllowlist(enabled=True, numbers="919876543210, 919000000001")

    assert guard.allows("919876543210")
    assert guard.allows("919000000001")
    assert not guard.allows("919000000002")


def test_empty_allowlist_blocks_everyone() -> None:
    """Fail-closed: silence is recoverable, messaging a customer is not."""
    guard = PhoneAllowlist(enabled=True, numbers="")

    assert guard.blocks_everyone
    assert not guard.allows("919876543210")


def test_disabled_allowlist_permits_everyone() -> None:
    """The production posture, reached only by explicitly turning the guard off."""
    guard = PhoneAllowlist(enabled=False, numbers="")

    assert guard.allows("918888800000")


def test_short_numbers_cannot_match_loosely() -> None:
    """A suffix match on a short string would be a dangerous false positive."""
    guard = PhoneAllowlist(enabled=True, numbers="3210")

    assert not guard.allows("919876543210")


@pytest.mark.asyncio
async def test_guarded_client_refuses_an_unlisted_recipient() -> None:
    inner = LoggingMessagingClient()
    guarded = GuardedMessagingClient(inner, PhoneAllowlist(enabled=True, numbers="919876543210"))

    assert await guarded.send("918888800000", OutboundMessage(text="hi")) is None
    assert inner.sent == []
    assert guarded.blocked == ["918888800000"]


@pytest.mark.asyncio
async def test_guarded_client_lets_a_listed_recipient_through() -> None:
    inner = LoggingMessagingClient()
    guarded = GuardedMessagingClient(inner, PhoneAllowlist(enabled=True, numbers="919876543210"))

    await guarded.send("919876543210", OutboundMessage(text="hi"))

    assert [to for to, _ in inner.sent] == ["919876543210"]
