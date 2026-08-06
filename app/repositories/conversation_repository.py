"""Persistence for `Conversation`.

This is the concrete implementation of the conversation-state store. The
`ConversationStore` protocol in `app.services.state` describes the contract; a
Redis-backed implementation can be dropped in later without touching the bot.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import Conversation
from app.domain.enums import ConversationState


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, user_id: int, *, for_update: bool = False) -> Conversation | None:
        """Return the user's open conversation.

        `for_update` takes a row lock so two messages that arrive at the same
        moment cannot both advance the state machine from the same starting
        state. PostgreSQL honours this; SQLite (tests) ignores it harmlessly.
        """
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id, Conversation.is_active.is_(True))
            .order_by(Conversation.id.desc())
            .limit(1)
        )
        bind = self._session.bind
        if for_update and bind is not None and bind.dialect.name == "postgresql":
            # `of=` is not optional here. `Conversation.user` is `lazy="joined"`,
            # so this statement carries a LEFT OUTER JOIN to `users`, and a bare
            # FOR UPDATE would try to lock the nullable side of it - which
            # PostgreSQL rejects outright ("FOR UPDATE cannot be applied to the
            # nullable side of an outer join"). Naming the table locks the
            # conversation row only, which is all the state machine needs.
            stmt = stmt.with_for_update(of=Conversation)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def create(self, user_id: int) -> Conversation:
        conversation = Conversation(
            user_id=user_id,
            current_state=ConversationState.START,
            context={},
        )
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def get_or_create_active(
        self, user_id: int, *, for_update: bool = False
    ) -> Conversation:
        conversation = await self.get_active(user_id, for_update=for_update)
        if conversation is None:
            conversation = await self.create(user_id)
        return conversation

    async def set_state(
        self, conversation: Conversation, state: ConversationState
    ) -> Conversation:
        conversation.current_state = state
        conversation.last_activity_at = datetime.now(UTC)
        await self._session.flush()
        return conversation

    async def close(self, conversation: Conversation) -> Conversation:
        """Retire a finished thread; the next message starts a fresh one."""
        conversation.is_active = False
        conversation.current_state = ConversationState.END
        await self._session.flush()
        return conversation

    async def touch(self, conversation: Conversation, last_message: str | None = None) -> None:
        conversation.last_activity_at = datetime.now(UTC)
        if last_message is not None:
            conversation.last_message = last_message[:4000]
        await self._session.flush()
