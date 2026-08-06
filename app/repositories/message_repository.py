"""Persistence for `Message`."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.message import Message
from app.domain.enums import ConversationState, MessageSender


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        conversation_id: int,
        sender: MessageSender,
        message: str,
        wa_message_id: str | None = None,
        state: ConversationState | None = None,
    ) -> Message:
        record = Message(
            conversation_id=conversation_id,
            sender=sender,
            message=message,
            wa_message_id=wa_message_id,
            state=state,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def exists_wa_message(self, wa_message_id: str) -> bool:
        """Has this exact WhatsApp message already been processed?

        Meta retries a webhook until it receives a 200, so this check is what
        stops a user seeing the same reply twice.
        """
        result = await self._session.execute(
            select(Message.id).where(Message.wa_message_id == wa_message_id).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def recent(self, conversation_id: int, limit: int = 10) -> list[Message]:
        """Last `limit` messages in chronological order (oldest first)."""
        result = await self._session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))
