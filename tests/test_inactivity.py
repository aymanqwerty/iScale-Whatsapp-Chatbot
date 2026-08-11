"""The inactivity nudges.

Two messages, an hour and then seven hours after the customer goes quiet, and
nothing is ever closed. Most of these test who must NOT be messaged: a wrong
nudge pesters a customer who already booked, or talks over a human agent - both
far worse than missing one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.bot import copy
from app.services.inactivity import (
    CTX_INACTIVITY_AT,
    CTX_INACTIVITY_SENT,
    CTX_INACTIVITY_STAGE,
    InactivitySweeper,
)
from tests.conftest import Harness

#: Comfortably past the one-hour first threshold.
QUIET = 90
#: Past the six-hour gap between the two nudges.
LONG_GAP = 60 * 7


def _sweeper(harness: Harness) -> InactivitySweeper:
    return InactivitySweeper(
        database=harness.database,
        messaging=harness.messaging,
        settings=harness.service._settings,
    )


async def _conversation(harness: Harness):
    from app.repositories.conversation_repository import ConversationRepository
    from app.repositories.user_repository import UserRepository

    async with harness.database.session() as session:
        user = await UserRepository(session).get_by_phone(harness.phone)
        assert user is not None
        return await ConversationRepository(session).get_active(user.id)


async def _go_quiet(harness: Harness, minutes: int = QUIET) -> None:
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


async def _age_the_nudge(harness: Harness, minutes: int) -> None:
    """Backdate when the last nudge was sent, so the next becomes due."""
    from app.repositories.conversation_repository import ConversationRepository
    from app.repositories.user_repository import UserRepository

    async with harness.database.session() as session:
        user = await UserRepository(session).get_by_phone(harness.phone)
        assert user is not None
        conversation = await ConversationRepository(session).get_active(user.id)
        assert conversation is not None
        conversation.set_ctx(
            CTX_INACTIVITY_AT,
            (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(),
        )
        await session.commit()


def _last_text(harness: Harness) -> str:
    return harness.messaging.sent[-1][1].text


# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #
async def test_nothing_is_sent_before_the_first_hour(harness: Harness) -> None:
    """Half an hour is someone reading, not someone gone."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness, minutes=30)

    assert await _sweeper(harness).sweep() == 0


async def test_the_first_nudge_goes_after_an_hour(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)

    assert await _sweeper(harness).sweep() == 1
    assert "went quiet on me" in _last_text(harness)


async def test_the_second_waits_for_the_six_hour_gap(harness: Harness) -> None:
    """A second nudge an hour later would be pestering, not helping."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)
    sweeper = _sweeper(harness)

    assert await sweeper.sweep() == 1
    assert await sweeper.sweep() == 0, "second nudge fired immediately"

    await _age_the_nudge(harness, LONG_GAP)
    assert await sweeper.sweep() == 1


async def test_there_is_never_a_third(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)
    sweeper = _sweeper(harness)

    assert await sweeper.sweep() == 1
    await _age_the_nudge(harness, LONG_GAP)
    assert await sweeper.sweep() == 1
    await _age_the_nudge(harness, LONG_GAP * 10)

    assert await sweeper.sweep() == 0


async def test_the_last_nudge_says_it_is_the_last(harness: Harness) -> None:
    """Someone who ignored two messages deserves to know a third is not coming."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)
    sweeper = _sweeper(harness)
    await sweeper.sweep()
    await _age_the_nudge(harness, LONG_GAP)

    await sweeper.sweep()

    assert "one last time" in _last_text(harness)


# --------------------------------------------------------------------------- #
# Nothing is closed - the whole point of the feature
# --------------------------------------------------------------------------- #
async def test_the_conversation_stays_open(harness: Harness) -> None:
    """Closing would start a fresh conversation on their next message."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)

    await _sweeper(harness).sweep()

    assert await harness.state() != "CLOSED"


async def test_everything_captured_survives_the_nudge(harness: Harness) -> None:
    """The customer must be able to carry on exactly where they stopped."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=copy.GROUP_COHORT)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}ai-for-everyone")
    await harness.say("i am a doctor")

    before = await _conversation(harness)
    assert before is not None
    state_before = str(before.current_state)
    course_before = before.current_course
    profile_before = before.get_ctx("profile")
    assert course_before and profile_before, "nothing captured to protect"

    await _go_quiet(harness)
    await _sweeper(harness).sweep()

    after = await _conversation(harness)
    assert after is not None
    assert str(after.current_state) == state_before
    assert after.current_course == course_before
    assert after.get_ctx("profile") == profile_before


async def test_replying_continues_the_same_thread(harness: Harness) -> None:
    """The model must still see what was said before the nudge."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=copy.GROUP_COHORT)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}ai-for-everyone")
    await harness.say("i am a doctor")
    before = await _conversation(harness)
    assert before is not None

    await _go_quiet(harness)
    await _sweeper(harness).sweep()
    await harness.say("sorry, was busy - what about the fees?")

    after = await _conversation(harness)
    assert after is not None
    assert after.id == before.id, "a new conversation was started"
    history = str(harness.llm.calls[-1].get("history", ""))
    assert "doctor" in history, "the earlier conversation was lost"


async def test_the_nudge_does_not_reset_the_inactivity_clock(
    harness: Harness,
) -> None:
    """`last_activity_at` must keep meaning "when the CUSTOMER last spoke".

    Moving it would push the 24-hour window out and postpone the second nudge
    by an hour every time the first one fired.
    """
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)
    before = await _conversation(harness)
    assert before is not None
    stamp = before.last_activity_at

    await _sweeper(harness).sweep()

    after = await _conversation(harness)
    assert after is not None
    assert after.last_activity_at == stamp


async def test_the_nudge_is_in_the_transcript(harness: Harness) -> None:
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
    assert "went quiet on me" in last.message


# --------------------------------------------------------------------------- #
# Who must not be nudged
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
            await session.execute(
                select(Conversation).order_by(desc(Conversation.id)).limit(1)
            )
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
    await _go_quiet(harness, minutes=60 * 40)

    assert await _sweeper(harness).sweep() == 0


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #
async def test_an_abandoned_booking_gets_the_tailored_message(
    harness: Harness,
) -> None:
    """The most recoverable lead in the funnel deserves better than "hello?"."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    assert await harness.state() == "ASK_NAME"
    await _go_quiet(harness)

    assert await _sweeper(harness).sweep() == 1
    assert "getting your call booked" in _last_text(harness)


async def test_the_stage_is_persisted(harness: Harness) -> None:
    """Held in the database, so a restart does not start the count again."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await _go_quiet(harness)
    await _sweeper(harness).sweep()

    conversation = await _conversation(harness)
    assert conversation is not None
    assert conversation.get_ctx(CTX_INACTIVITY_STAGE) == 1
    assert conversation.get_ctx(CTX_INACTIVITY_AT)


async def test_a_conversation_chased_by_the_old_version_is_not_chased_again(
    harness: Harness,
) -> None:
    """Rows written before this change carry a boolean, not a stage.

    Reading it as "already nudged once" stops the deploy re-nudging everyone who
    was quiet at that moment.
    """
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    conversation = await _conversation(harness)
    assert conversation is not None

    async with harness.database.session() as session:
        from app.db.models.conversation import Conversation as Row

        row = await session.get(Row, conversation.id)
        assert row is not None
        row.set_ctx(CTX_INACTIVITY_SENT, True)
        await session.commit()
    await _go_quiet(harness)

    # Treated as stage 1, and with no timestamp the second is never due.
    assert await _sweeper(harness).sweep() == 0


async def test_it_can_be_switched_off(harness: Harness) -> None:
    settings = harness.service._settings.model_copy(
        update={"inactivity_enabled": False}
    )
    sweeper = InactivitySweeper(
        database=harness.database, messaging=harness.messaging, settings=settings
    )
    sweeper.start()

    assert sweeper._task is None
    await sweeper.stop()
