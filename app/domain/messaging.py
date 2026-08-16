"""Channel-agnostic message value objects.

The bot builds `OutboundMessage`s and never touches the WhatsApp JSON shape.
Adding a second channel (web widget, Telegram) means writing one more renderer,
not rewriting the conversation logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.enums import MessageKind

# WhatsApp Cloud API interactive-message limits.
MAX_BUTTONS = 3
MAX_LIST_ROWS = 10
BUTTON_TITLE_LIMIT = 20
ROW_TITLE_LIMIT = 24
ROW_DESCRIPTION_LIMIT = 72
BODY_TEXT_LIMIT = 1024
TEXT_LIMIT = 4096


def _truncate(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A normalised message received from the user."""

    wa_message_id: str
    from_phone: str
    kind: MessageKind
    text: str = ""
    #: Set when the user tapped a button or list row; carries our own option id.
    reply_id: str | None = None
    profile_name: str | None = None
    timestamp: datetime | None = None
    #: WhatsApp's own type string for payloads we cannot read as text
    #: ("image", "audio", "document"...). Carried so a payment screenshot can be
    #: told apart from a voice note - both are UNSUPPORTED, but only one of them
    #: is somebody trying to pay us.
    media_type: str | None = None
    #: Cloud API's handle for the attachment's bytes. The webhook carries no URL
    #: and there is no WhatsApp app on our side to open the picture in, so this
    #: id is the only route to it - fetched through the Graph API with the
    #: access token and stored, or the image is lost when Meta expires it.
    media_id: str | None = None
    media_mime: str | None = None

    @property
    def is_image(self) -> bool:
        """Whether this is a picture - the shape a payment proof arrives in."""
        return self.media_type in ("image", "document", "sticker")

    @property
    def is_actionable(self) -> bool:
        """False for stickers, media and other payloads the MVP ignores."""
        return self.kind is not MessageKind.UNSUPPORTED and bool(self.text or self.reply_id)

    @property
    def normalized_text(self) -> str:
        return self.text.strip().lower()


@dataclass(frozen=True, slots=True)
class Button:
    """A quick-reply button. WhatsApp allows at most three per message."""

    id: str
    title: str

    def rendered_title(self) -> str:
        return _truncate(self.title, BUTTON_TITLE_LIMIT)


@dataclass(frozen=True, slots=True)
class ListRow:
    """A row inside an interactive list. At most ten per message."""

    id: str
    title: str
    description: str = ""

    def rendered_title(self) -> str:
        return _truncate(self.title, ROW_TITLE_LIMIT)

    def rendered_description(self) -> str:
        return _truncate(self.description, ROW_DESCRIPTION_LIMIT)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """A reply to send.

    Rendering rule, applied by the channel client:
      * `buttons` present -> interactive button message
      * `list_rows` present -> interactive list message
      * otherwise -> plain text
    """

    text: str
    buttons: tuple[Button, ...] = ()
    list_rows: tuple[ListRow, ...] = ()
    list_button_label: str = "Choose"
    header: str | None = None
    footer: str | None = None

    def __post_init__(self) -> None:
        if self.buttons and self.list_rows:
            raise ValueError("A message may carry buttons or list rows, not both")
        if len(self.buttons) > MAX_BUTTONS:
            raise ValueError(f"WhatsApp allows at most {MAX_BUTTONS} buttons")
        if len(self.list_rows) > MAX_LIST_ROWS:
            raise ValueError(f"WhatsApp allows at most {MAX_LIST_ROWS} list rows")

    @property
    def is_interactive(self) -> bool:
        return bool(self.buttons or self.list_rows)

    def rendered_text(self) -> str:
        limit = BODY_TEXT_LIMIT if self.is_interactive else TEXT_LIMIT
        return _truncate(self.text, limit)

    @property
    def options(self) -> tuple[tuple[str, str], ...]:
        """(id, title) pairs, whichever widget was used - handy for tests."""
        if self.buttons:
            return tuple((b.id, b.title) for b in self.buttons)
        return tuple((r.id, r.title) for r in self.list_rows)


@dataclass(slots=True)
class TurnResult:
    """Everything the state machine decided for one inbound message."""

    replies: list[OutboundMessage] = field(default_factory=list)
    #: Set when the turn produced a lead, so the caller can trigger CRM sync.
    lead_id: int | None = None
    #: True when the conversation finished and should be retired.
    close_conversation: bool = False

    def add(self, message: OutboundMessage) -> None:
        self.replies.append(message)
