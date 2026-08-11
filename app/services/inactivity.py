"""Follow up once when a conversation goes quiet, then close it.

Someone who stops replying mid-flow is usually distracted rather than
uninterested, and a half-finished booking is the most recoverable lead there
is. One check-in gets them back; a sequence of them is harassment.

Everything here is about who NOT to message. The rules are deliberately
conservative, because the cost of a wrong follow-up (pestering a customer who
already booked, or talking over a human agent) is far higher than the cost of
missing one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select

from app.bot import copy, intents
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.models.user import User
from app.db.session import Database
from app.domain.enums import (
    CALLBACK_CAPTURE_STATES,
    ConversationState,
    MessageSender,
)
from app.domain.messaging import OutboundMessage
from app.services.whatsapp.base import MessagingClient

logger = get_logger(__name__)

#: Marks a conversation as already chased. One follow-up, ever.
CTX_INACTIVITY_SENT = "inactivity_sent"

#: States that mean the conversation reached its end. Someone whose call is
#: booked does not need chasing - they need leaving alone.
_FINISHED: frozenset[ConversationState] = frozenset(
    {ConversationState.LEAD_CREATED, ConversationState.END}
)


class InactivitySweeper:
    """Periodically checks for stalled conversations and follows up once."""

    def __init__(
        self,
        *,
        database: Database,
        messaging: MessagingClient,
        settings: Settings,
    ) -> None:
        self._database = database
        self._messaging = messaging
        self._settings = settings
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if not self._settings.inactivity_enabled:
            logger.info("Inactivity follow-up disabled")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Inactivity follow-up active",
            extra={
                "after_minutes": self._settings.inactivity_minutes,
                "every_seconds": self._settings.inactivity_sweep_seconds,
            },
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        interval = max(self._settings.inactivity_sweep_seconds, 10)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A sweep failure must never kill the loop - the next one may
                # well succeed, and a dead sweeper is silent for the rest of the
                # process's life.
                logger.exception("Inactivity sweep failed")

    # ------------------------------------------------------------------ #
    async def sweep(self) -> int:
        """Follow up on every stalled conversation. Returns how many were sent."""
        now = datetime.now(UTC)
        quiet_since = now - timedelta(minutes=self._settings.inactivity_minutes)
        too_old = now - timedelta(hours=self._settings.inactivity_max_age_hours)

        sent = 0
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(Conversation, User)
                    .join(User, User.id == Conversation.user_id)
                    .where(
                        Conversation.is_active.is_(True),
                        Conversation.last_activity_at <= quiet_since,
                        # Outside WhatsApp's window the send is rejected anyway.
                        Conversation.last_activity_at >= too_old,
                        Conversation.current_state.not_in(tuple(_FINISHED)),
                        # A human is handling this one; the bot must stay quiet.
                        User.bot_paused.is_(False),
                    )
                    .limit(200)
                )
            ).all()

            for conversation, user in rows:
                if conversation.get_ctx(CTX_INACTIVITY_SENT, False):
                    continue
                if not await self._is_waiting_on_them(session, conversation):
                    continue

                text = _follow_up_text(conversation, user)
                try:
                    await self._messaging.send(user.phone, OutboundMessage(text=text))
                except Exception:
                    # Leave the flag unset so the next sweep retries. A failed
                    # send is usually the 24-hour window closing, which the age
                    # filter above will exclude shortly anyway.
                    logger.warning(
                        "Inactivity follow-up could not be delivered",
                        extra={"phone": _mask(user.phone)},
                    )
                    continue

                session.add(
                    Message(
                        conversation_id=conversation.id,
                        sender=MessageSender.BOT,
                        message=text,
                        state=conversation.current_state,
                        timestamp=datetime.now(UTC),
                    )
                )
                conversation.set_ctx(CTX_INACTIVITY_SENT, True)
                # Closed straight away: the message says so, and the next thing
                # they send should start cleanly rather than resuming a form
                # they abandoned twenty minutes ago.
                conversation.is_active = False
                conversation.current_state = ConversationState.END
                sent += 1

            if sent:
                await session.commit()
                logger.info("Inactivity follow-ups sent", extra={"count": sent})

        return sent

    # ------------------------------------------------------------------ #
    @staticmethod
    async def _is_waiting_on_them(session: object, conversation: Conversation) -> bool:
        """True only if the bot spoke last and was not being said goodbye to.

        Two exclusions, both learned from real transcripts. If the *user* spoke
        last, the silence is ours, not theirs - chasing them would be absurd.
        And a last message of "ok thanks" is a completed conversation, not an
        abandoned one; the same detector the greeting path uses catches it.
        """
        last = (
            await session.execute(  # type: ignore[attr-defined]
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(desc(Message.id))
                .limit(1)
            )
        ).scalar_one_or_none()

        if last is None or last.sender is MessageSender.USER:
            return False
        # An agent's message means a human is mid-conversation.
        if last.sender is MessageSender.AGENT:
            return False

        recent_user = (
            await session.execute(  # type: ignore[attr-defined]
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.sender == MessageSender.USER,
                )
                .order_by(desc(Message.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if recent_user is not None and intents.is_closing_remark(recent_user.message):
            return False
        return True


def _follow_up_text(conversation: Conversation, user: User) -> str:
    """Tailored to where they stopped.

    A half-captured booking is the most recoverable thing in the funnel, and a
    generic "are you still there?" wastes it - so that case names what was left
    unfinished instead.
    """
    name = (user.name or "").split()[0] if user.name else ""
    if conversation.current_state in CALLBACK_CAPTURE_STATES:
        return copy.INACTIVITY_BOOKING.format(greeting=_greeting(name))
    return copy.INACTIVITY_GENERAL.format(greeting=_greeting(name))


def _greeting(name: str) -> str:
    return f"Hi {name}" if name else "Hi"


def _mask(phone: str) -> str:
    return f"{phone[:4]}***{phone[-3:]}" if len(phone) > 7 else "***"
