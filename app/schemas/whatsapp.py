"""Pydantic models for the WhatsApp Cloud API webhook payload.

Every field is optional because Meta sends several unrelated event shapes
(messages, delivery statuses, template updates) through the same endpoint.
Validation here is deliberately permissive - the parser decides what is
actionable, and an unknown shape must never 500 the webhook or Meta will start
retrying and eventually disable it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Profile(_Lenient):
    name: str | None = None


class Contact(_Lenient):
    wa_id: str | None = None
    profile: Profile | None = None


class TextBody(_Lenient):
    body: str | None = None


class MediaBody(_Lenient):
    """An image, document or other attachment.

    Only the id and mime type are carried: Cloud API keeps the bytes on Meta's
    servers behind the access token, so the id is the only handle we get.
    """

    id: str | None = None
    mime_type: str | None = None
    caption: str | None = None


class Reply(_Lenient):
    """Shared shape of `button_reply` and `list_reply`."""

    id: str | None = None
    title: str | None = None
    description: str | None = None


class Interactive(_Lenient):
    type: str | None = None
    button_reply: Reply | None = None
    list_reply: Reply | None = None


class ButtonPayload(_Lenient):
    """Reply from a template quick-reply button (distinct from `interactive`)."""

    text: str | None = None
    payload: str | None = None


class WhatsAppMessage(_Lenient):
    id: str | None = None
    from_: str | None = Field(default=None, alias="from")
    timestamp: str | None = None
    type: str | None = None
    text: TextBody | None = None
    interactive: Interactive | None = None
    button: ButtonPayload | None = None
    image: MediaBody | None = None
    document: MediaBody | None = None


class Status(_Lenient):
    """Delivery receipt - acknowledged and ignored by the MVP."""

    id: str | None = None
    status: str | None = None
    recipient_id: str | None = None


class Metadata(_Lenient):
    display_phone_number: str | None = None
    phone_number_id: str | None = None


class ChangeValue(_Lenient):
    messaging_product: str | None = None
    metadata: Metadata | None = None
    contacts: list[Contact] = Field(default_factory=list)
    messages: list[WhatsAppMessage] = Field(default_factory=list)
    statuses: list[Status] = Field(default_factory=list)


class Change(_Lenient):
    field: str | None = None
    value: ChangeValue | None = None


class Entry(_Lenient):
    id: str | None = None
    changes: list[Change] = Field(default_factory=list)


class WebhookPayload(_Lenient):
    object: str | None = None
    entry: list[Entry] = Field(default_factory=list)
