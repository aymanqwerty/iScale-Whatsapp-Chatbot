"""Lead - a callback request handed over to a human counselor."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.domain.enums import LeadStatus, LeadType, SyncStatus

if TYPE_CHECKING:
    from app.db.models.conversation import Conversation
    from app.db.models.user import User


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True, nullable=True
    )

    type: Mapped[LeadType] = mapped_column(
        Enum(LeadType, native_enum=False, length=16), nullable=False, index=True
    )
    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, native_enum=False, length=16),
        default=LeadStatus.NEW,
        nullable=False,
        index=True,
    )

    #: Denormalised so the sheet row and any CRM export stay correct even if the
    #: user later changes their name on a different conversation.
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: The WhatsApp sender id. Always present - it is how the message arrived.
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: The number the user actually wants to be called on. Usually the same as
    #: `phone`, but kept separate: a counselor ringing the WhatsApp number when
    #: the user asked for their office line is the kind of small failure that
    #: loses a sale, and overwriting `phone` would lose the thread's identity.
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Post-sales only, and mandatory there - a support call is not booked
    #: without it, because it is the only thing tying the request to an account.
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    interested_course: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Post-sales: which course they say they are enrolled in.
    enrolled_course: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: What the user said they do ("final year B.Tech student"). Pre-sales
    #: context so a counselor opens the call already knowing who they are.
    profession: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Routing label from the support menu: Video Related, Technical Issue, Other.
    issue_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Resolved callback slot, stored as an absolute UTC instant.
    preferred_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Exactly what the user typed, kept for the counselor's benefit.
    preferred_time_raw: Mapped[str | None] = mapped_column(String(255), nullable=True)

    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    sync_status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, native_enum=False, length=16),
        default=SyncStatus.PENDING,
        nullable=False,
    )
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="leads", lazy="joined")
    conversation: Mapped[Conversation | None] = relationship(back_populates="leads")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Lead id={self.id} type={self.type} status={self.status}>"
