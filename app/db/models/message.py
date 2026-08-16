"""Message - full transcript of every turn, inbound and outbound."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.domain.enums import ConversationState, MessageSender

if TYPE_CHECKING:
    from app.db.models.conversation import Conversation


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )

    sender: Mapped[MessageSender] = mapped_column(
        Enum(MessageSender, native_enum=False, length=16), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)

    #: WhatsApp's own message id. Unique, and the basis of webhook de-duplication:
    #: Meta re-delivers a webhook until it gets a 200, so the same message can
    #: legitimately arrive more than once.
    wa_message_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )

    #: State the machine was in when this message was processed - invaluable when
    #: debugging a conversation after the fact.
    state: Mapped[ConversationState | None] = mapped_column(
        Enum(ConversationState, native_enum=False, length=32), nullable=True
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    #: An attachment's bytes, stored because there is nowhere else to keep them.
    #: Cloud API holds media on Meta's servers behind the access token and
    #: expires it, and there is no WhatsApp app on our side to open it in - so
    #: an unstored payment screenshot is simply lost.
    #:
    #: Only saved for payment proofs, which bounds this to people who reached
    #: the checkout step. Postgres moves anything this large out to TOAST
    #: storage, so the column costs nothing on the ~99% of rows where it is
    #: NULL, and the transcript queries never select it.
    media_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    media_mime: Mapped[str | None] = mapped_column(String(80), nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    @property
    def has_media(self) -> bool:
        return self.media_data is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Message id={self.id} sender={self.sender}>"
