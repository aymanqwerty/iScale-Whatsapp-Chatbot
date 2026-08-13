"""Nudge a conversation that has gone quiet. Twice at most, and never close it.

Someone who stops replying mid-flow is usually distracted rather than
uninterested, and a half-finished booking is the most recoverable lead there
is. Two check-ins get them back; a third would be harassment.

NOTHING IS CLOSED. The conversation keeps its state, its course, its captured
details and its history, so replying six hours later continues the same thread.
Marking it inactive would create a fresh conversation on the next message - and
because history is loaded per conversation, the model would then greet a
half-known customer as a stranger. The point of these messages is to bring
someone back, which is the opposite of discarding everything they told us.

Everything else here is about who NOT to message. The rules are deliberately
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

#: Nudges sent so far: absent/0, 1, or 2. Two is the end of it - the copy for
#: the second says so, and a third would be pestering.
CTX_INACTIVITY_STAGE = "inactivity_stage"

#: When the last nudge went out, ISO-8601. The second is timed from this rather
#: than from the customer's last message, so "six hours later" means six hours
#: after we spoke - which is how it reads.
CTX_INACTIVITY_AT = "inactivity_at"

#: The previous single-nudge flag. Still read, so a conversation already chased
#: before this change is not chased again the moment it deploys.
CTX_INACTIVITY_SENT = "inactivity_sent"

MAX_STAGE = 2

#: States that mean the conversation reached its end. Someone whose call is
#: booked does not need chasing - they need leaving alone.
_FINISHED: frozenset[ConversationState] = frozenset(
    {ConversationState.LEAD_CREATED, ConversationState.END}
)


class InactivitySweeper:
    """Periodically checks for stalled conversations and nudges them."""

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
                "first_after_minutes": self._settings.inactivity_minutes,
                "second_after_minutes": self._settings.inactivity_followup_minutes,
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
        """Nudge every stalled conversation that is due. Returns how many."""
        now = datetime.now(UTC)
        first_due = now - timedelta(minutes=self._settings.inactivity_minutes)
        too_old = now - timedelta(hours=self._settings.inactivity_max_age_hours)
        gap = timedelta(minutes=self._settings.inactivity_followup_minutes)

        sent = 0
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(Conversation, User)
                    .join(User, User.id == Conversation.user_id)
                    .where(
                        Conversation.is_active.is_(True),
                        Conversation.last_activity_at <= first_due,
                        # Outside WhatsApp's window the send is rejected anyway.
                        Conversation.last_activity_at >= too_old,
                        Conversation.current_state.not_in(tuple(_FINISHED)),
                        # A human is handling this one; the bot must stay quiet.
                        User.bot_paused.is_(False),
                        # Blocked outright. Chasing someone an agent blocked is
                        # the single worst message this sweeper could send.
                        User.blocked.is_(False),
                    )
                    .limit(200)
                )
            ).all()

            for conversation, user in rows:
                stage = _stage_of(conversation)
                if stage >= MAX_STAGE:
                    continue
                if stage == 1 and not _due_for_second(conversation, now, gap):
                    continue
                if not await self._is_waiting_on_them(session, conversation):
                    continue

                text = _follow_up_text(conversation, user, stage + 1)
                try:
                    await self._messaging.send(user.phone, OutboundMessage(text=text))
                except Exception:
                    # Leave the stage unchanged so the next sweep retries. A
                    # failed send is usually the 24-hour window closing, which
                    # the age filter above excludes shortly anyway.
                    logger.warning(
                        "Inactivity follow-up could not be delivered",
                        extra={"phone": _mask(user.phone), "stage": stage + 1},
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
                conversation.set_ctx(CTX_INACTIVITY_STAGE, stage + 1)
                conversation.set_ctx(CTX_INACTIVITY_AT, datetime.now(UTC).isoformat())
                # Deliberately NOT touching is_active, current_state or
                # last_activity_at. The conversation must stay exactly where it
                # was so the customer can carry on, and last_activity_at must
                # keep meaning "when the CUSTOMER was last active" - moving it
                # would push the 24-hour window out and reset the schedule.
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


def _follow_up_text(conversation: Conversation, user: User, stage: int) -> str:
    """Tailored to where they stopped, and to which nudge this is.

    A half-captured booking is the most recoverable thing in the funnel, so that
    case names what was left unfinished rather than asking a generic "are you
    still there?". The second nudge also says it is the last - someone who has
    ignored two messages deserves to know a third is not coming.
    """
    name = (user.name or "").split()[0] if user.name else ""
    greeting = _greeting(name)
    mid_booking = conversation.current_state in CALLBACK_CAPTURE_STATES

    if stage >= MAX_STAGE:
        template = (
            copy.INACTIVITY_BOOKING_LAST
            if mid_booking
            else copy.INACTIVITY_GENERAL_LAST
        )
    else:
        template = copy.INACTIVITY_BOOKING if mid_booking else copy.INACTIVITY_GENERAL
    return template.format(greeting=greeting)


def _stage_of(conversation: Conversation) -> int:
    """How many nudges this conversation has had, tolerating the older flag."""
    try:
        stage = int(conversation.get_ctx(CTX_INACTIVITY_STAGE, 0) or 0)
    except (TypeError, ValueError):  # pragma: no cover - we write this ourselves
        stage = 0
    if stage == 0 and conversation.get_ctx(CTX_INACTIVITY_SENT, False):
        # Chased once by the previous single-nudge version.
        return 1
    return stage


def _due_for_second(conversation: Conversation, now: datetime, gap: timedelta) -> bool:
    """Whether enough time has passed since the first nudge."""
    raw = conversation.get_ctx(CTX_INACTIVITY_AT)
    if not raw:
        # No timestamp - a row written by the previous version. Treating it as
        # not yet due is the safe direction: the second nudge never fires,
        # rather than firing the instant this deploys.
        return False
    try:
        sent_at = datetime.fromisoformat(str(raw))
    except ValueError:  # pragma: no cover - we write this ourselves
        return False
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    return now - sent_at >= gap


def _greeting(name: str) -> str:
    return f"Hi {name}" if name else "Hi"


def _mask(phone: str) -> str:
    return f"{phone[:4]}***{phone[-3:]}" if len(phone) > 7 else "***"
