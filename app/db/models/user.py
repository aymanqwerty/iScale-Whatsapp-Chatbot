"""The person on the other end of the WhatsApp thread."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String
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
