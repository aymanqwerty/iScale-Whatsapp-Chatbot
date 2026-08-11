"""The inactivity follow-up.

Most of these test who must NOT be messaged. A wrong follow-up pesters a
customer who already booked, or talks over a human agent mid-conversation -
both far worse than missing one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.bot import copy
from app.services.inactivity import CTX_INACTIVITY_SENT, InactivitySweeper
from tests.conftest import Harness


def _sweeper(harness: Harness) -> InactivitySweeper:
    return InactivitySweeper(
        database=harness.database,
        messaging=harness.messaging,
        settings=harness.service._settings,
    )


async def _go_quiet(harness: Harness, minutes: int = 30) -> None:
    """Backdate the conversation so it looks abandoned."""
    from app.repositories.conversation_repository import ConversationRepository
    from app.repositories.user_repository import UserRepository

    async with harness.database.session() as session:
        user = await UserRepository(session).get_by_phone(harness.phone)
        assert user is not None
        conversation = await ConversationRepository(session).get_active(user.id)
        if conversation is None:
            return
        conversation.last_activity_at = datetime.now(UTC) - timedelta(minutes=minutes)
        await session.commit()


async def _state(harness: Harness) -> str:
    return await harness.state()


# --------------------------------------------------------------------------- #
# It fires when it should
# --------------------------------------------------------------------------- #
async def test_an_abandoned_booking_is_followed_up(harness: Harness) -> None:
    """The most recoverable lead in the funnel: they stopped partway through."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    assert await _state(harness) == "ASK_NAME"
    await _go_quiet(harness)

    before = len(harness.messaging.sent)
    sent = await _sweeper(harness).sweep()

    assert sent == 1
    text = harness.messaging.sent[-1][1].text
    assert "booking your call" in text, "a generic nudge wastes an abandoned booking"
    assert len(harness.messaging.sent) == before + 1


async def test_an_abandoned_question_gets_the_general_message(
    harness: Harness,
) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)

    assert await _sweeper(harness).sweep() == 1

    text = harness.messaging.sent[-1][1].text
    assert "close this chat for now" in text


async def test_the_conversation_is_closed_afterwards(harness: Harness) -> None:
    """The message says the chat is closing, so it must actually close."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)

    await _sweeper(harness).sweep()

    assert await _state(harness) == "CLOSED"


async def test_the_follow_up_is_in_the_transcript(harness: Harness) -> None:
    """The console must show what the customer was actually sent."""
    from sqlalchemy import desc, select

    from app.db.models.message import Message

    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)
    await _sweeper(harness).sweep()

    async with harness.database.session() as session:
        last = (
            await session.execute(select(Message).order_by(desc(Message.id)).limit(1))
        ).scalar_one()
    assert "close this chat" in last.message


# --------------------------------------------------------------------------- #
# It must not fire
# --------------------------------------------------------------------------- #
async def test_a_completed_booking_is_left_alone(harness: Harness) -> None:
    """Chasing someone whose call is already booked is pure annoyance."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    await harness.give_name("Meera")
    await harness.say("tomorrow 2 pm")
    await harness.say("skip")
    await _go_quiet(harness)

    assert await _sweeper(harness).sweep() == 0


async def test_someone_who_said_goodbye_is_left_alone(harness: Harness) -> None:
    """"ok thanks" is a finished conversation, not an abandoned one."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say("thanks")
    await _go_quiet(harness)

    assert await _sweeper(harness).sweep() == 0


async def test_a_handed_over_conversation_is_left_alone(harness: Harness) -> None:
    """A human is mid-conversation; the bot must not talk over them."""
    from app.repositories.user_repository import UserRepository

    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    async with harness.database.session() as session:
        user = await UserRepository(session).get_by_phone(harness.phone)
        assert user is not None
        user.bot_paused = True
        await session.commit()
    await _go_quiet(harness)

    assert await _sweeper(harness).sweep() == 0


async def test_nobody_is_followed_up_twice(harness: Harness) -> None:
    """One check-in. A sequence of them is harassment."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)
    sweeper = _sweeper(harness)

    assert await sweeper.sweep() == 1
    await _go_quiet(harness)
    assert await sweeper.sweep() == 0


async def test_a_recent_conversation_is_not_touched(harness: Harness) -> None:
    """Someone still reading must not be interrupted."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)

    assert await _sweeper(harness).sweep() == 0


async def test_waiting_on_us_is_not_their_silence(harness: Harness) -> None:
    """If the user spoke last, the silence is ours - chasing them is absurd."""
    from sqlalchemy import desc, select

    from app.db.models.conversation import Conversation
    from app.db.models.message import Message
    from app.domain.enums import MessageSender

    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    async with harness.database.session() as session:
        conversation = (
            await session.execute(select(Conversation).order_by(desc(Conversation.id)).limit(1))
        ).scalar_one()
        session.add(
            Message(
                conversation_id=conversation.id,
                sender=MessageSender.USER,
                message="are you there?",
                state=conversation.current_state,
                timestamp=datetime.now(UTC),
            )
        )
        await session.commit()
    await _go_quiet(harness)

    assert await _sweeper(harness).sweep() == 0


async def test_beyond_the_whatsapp_window_nobody_is_attempted(
    harness: Harness,
) -> None:
    """Outside 24 hours Meta rejects the send, so trying only logs failures."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness, minutes=60 * 40)   # 40 hours

    assert await _sweeper(harness).sweep() == 0


async def test_the_flag_survives_so_a_restart_does_not_re_send(
    harness: Harness,
) -> None:
    """The marker is persisted, not held in memory."""
    from sqlalchemy import desc, select

    from app.db.models.conversation import Conversation

    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)
    await _sweeper(harness).sweep()

    async with harness.database.session() as session:
        conversation = (
            await session.execute(select(Conversation).order_by(desc(Conversation.id)).limit(1))
        ).scalar_one()
    assert conversation.get_ctx(CTX_INACTIVITY_SENT) is True


# --------------------------------------------------------------------------- #
# Switch
# --------------------------------------------------------------------------- #
async def test_it_can_be_switched_off(harness: Harness) -> None:
    from dataclasses import replace as _replace  # noqa: F401

    settings = harness.service._settings.model_copy(
        update={"inactivity_enabled": False}
    )
    sweeper = InactivitySweeper(
        database=harness.database, messaging=harness.messaging, settings=settings
    )
    sweeper.start()

    assert sweeper._task is None
    await sweeper.stop()
