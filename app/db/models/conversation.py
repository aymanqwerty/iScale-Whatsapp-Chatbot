"""Conversation - the persisted state machine for one WhatsApp thread.

One active conversation per user. When a thread reaches `END` it is marked
inactive and the next inbound message opens a fresh one, which keeps a clean
audit trail instead of recycling rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONVariant, TimestampMixin
from app.domain.enums import ConversationState, LeadType

if TYPE_CHECKING:
    from app.db.models.lead import Lead
    from app.db.models.message import Message
    from app.db.models.user import User


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_active", "user_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    current_state: Mapped[ConversationState] = mapped_column(
        Enum(ConversationState, native_enum=False, length=32),
        default=ConversationState.START,
        nullable=False,
    )
    current_course: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lead_type: Mapped[LeadType | None] = mapped_column(
        Enum(LeadType, native_enum=False, length=16), nullable=True
    )

    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    #: Scratch space for the current flow - pending name, parsed callback time,
    #: how many questions have been answered, and so on. Keeping it in one JSON
    #: column means new flow steps do not require a migration.
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, default=dict, server_default="{}", nullable=False
    )

    user: Mapped[User] = relationship(back_populates="conversations", lazy="joined")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.id",
        lazy="selectin",
    )
    leads: Mapped[list[Lead]] = relationship(
        back_populates="conversation",
        lazy="selectin",
    )

    # ------------------------------------------------------------------ #
    # Context helpers - always reassign the dict so SQLAlchemy sees the change.
    # ------------------------------------------------------------------ #
    def get_ctx(self, key: str, default: Any = None) -> Any:
        return (self.context or {}).get(key, default)

    def set_ctx(self, key: str, value: Any) -> None:
        self.context = {**(self.context or {}), key: value}

    def update_ctx(self, **values: Any) -> None:
        self.context = {**(self.context or {}), **values}

    def clear_ctx(self, *keys: str) -> None:
        current = dict(self.context or {})
        for key in keys:
            current.pop(key, None)
        self.context = current

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Conversation id={self.id} state={self.current_state}>"
