"""The person on the other end of the WhatsApp thread."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.conversation import Conversation
    from app.db.models.lead import Lead


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: E.164 digits as delivered by WhatsApp (no '+'), unique per contact.
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)

    #: Name the user gave us during the callback flow (authoritative).
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: Name from the WhatsApp profile - a useful fallback, but user-editable.
    profile_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: A label an agent typed in the console. Deliberately NOT part of
    #: `display_name`: that is what the bot greets people by and prefills
    #: bookings with, so an internal note like "tyre kicker - do not chase"
    #: would end up addressed to the customer. Console display only.
    alias: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: When an agent pinned this contact to the top of the inbox. Null means
    #: unpinned; the timestamp orders several pinned rows most-recent-first.
    #: Shared, not per-agent - the console is one login for the whole team.
    pinned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: True while a human has taken this conversation over from the console.
    #: Held on the user rather than the conversation because conversations close
    #: when a lead is created - a handover must survive that, or the bot would
    #: silently start replying again mid-handover.
    bot_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: True when an agent has blocked this contact. Deliberately separate from
    #: `bot_paused`: a pause means "a human is answering instead", a block means
    #: "nobody answers, and nothing this number sends is even recorded". Keeping
    #: them apart means unblocking restores whatever the handover state was,
    #: rather than silently handing an awkward conversation back to the bot.
    blocked: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    blocked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Console username that blocked them. This is a shared internal login, so
    #: it is an audit breadcrumb rather than proof of who clicked.
    blocked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)

    #: When this contact last sent a payment screenshot. The console badges it
    #: so the team knows there is money to verify; an agent clears it once they
    #: have. Held on the user, not the conversation, because a payment must stay
    #: visible even if the conversation closes underneath it.
    payment_proof_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    leads: Mapped[list[Lead]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def display_name(self) -> str | None:
        return self.name or self.profile_name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User id={self.id} phone={self.phone!r}>"
