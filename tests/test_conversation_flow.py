"""End-to-end conversation flows through the real state machine."""

from __future__ import annotations

import pytest

from app.bot import copy
from app.core.exceptions import ConfigurationError, LLMError
from app.domain.enums import LeadStatus, LeadType, SyncStatus
from tests.conftest import Harness


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
async def test_greeting_opens_the_main_menu(harness: Harness) -> None:
    replies = await harness.say("Hi")

    assert len(replies) == 1
    assert "Welcome" in replies[0].text
    option_ids = {oid for oid, _ in replies[0].options}
    assert option_ids == {
        copy.MENU_COURSES,
        copy.MENU_ENROLLED,
        copy.MENU_COUNSELOR,
        copy.MENU_GENERAL,
    }
    assert await harness.state() == "MAIN_MENU"


async def test_returning_user_is_greeted_by_name(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    await harness.say("Priya Sharma")
    await harness.say("tomorrow 4pm")
    await harness.say("skip")

    replies = await harness.say("hello")

    assert "Priya Sharma" in replies[0].text


async def test_duplicate_webhook_delivery_is_ignored(harness: Harness) -> None:
    """Meta redelivers until it gets a 200; the same id must not reply twice."""
    from app.domain.enums import MessageKind
    from app.domain.messaging import InboundMessage

    inbound = InboundMessage(
        wa_message_id="wamid.duplicate",
        from_phone=harness.phone,
        kind=MessageKind.TEXT,
        text="hi",
    )

    await harness.service.process_inbound(inbound)
    first = len(harness.messaging.sent)
    await harness.service.process_inbound(inbound)

    assert len(harness.messaging.sent) == first


# --------------------------------------------------------------------------- #
# Pre-sales
# --------------------------------------------------------------------------- #
async def test_course_menu_is_built_from_the_knowledge_base(harness: Harness) -> None:
    await harness.say("hi")

    replies = await harness.say(reply_id=copy.MENU_COURSES)

    titles = {title for _, title in replies[0].options}
    assert "Data Analytics" in titles
    assert "Power BI" in titles
    assert "Not sure yet" in titles
    assert await harness.state() == "COURSE_SELECTION"


async def test_selecting_a_course_enters_qna(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)

    replies = await harness.say(reply_id=f"{copy.COURSE_PREFIX}data-science")

    assert "Data Science" in replies[0].text
    assert await harness.state() == "COURSE_QNA"


async def test_course_can_be_chosen_by_typing_its_name(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)

    await harness.say("i want power bi")

    assert await harness.state() == "COURSE_QNA"
    assert harness.llm.calls == []  # a selection, not a question


async def test_questions_are_answered_from_the_knowledge_base(
    harness: Harness,
) -> None:
    harness.llm.reply = "The Data Analytics program runs for 4 months."
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}data-analytics")

    replies = await harness.say("how long is the course?")

    assert replies[0].text == "The Data Analytics program runs for 4 months."
    prompt = harness.llm.calls[-1]["user_prompt"]
    assert "KNOWLEDGE" in prompt
    assert "4 months" in prompt  # the duration snippet was actually retrieved


async def test_callback_is_offered_after_the_nudge_threshold(
    harness: Harness,
) -> None:
    """Three answered questions, then one offer - and only one."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}data-analytics")

    await harness.say("what is the duration?")
    await harness.say("what tools are covered?")
    assert await harness.state() == "COURSE_QNA"

    replies = await harness.say("do you have weekend batches?")

    assert len(replies) == 2  # the answer, then the offer
    assert {oid for oid, _ in replies[1].options} == {copy.CONFIRM_YES, copy.CONFIRM_NO}
    assert await harness.state() == "ASK_CALLBACK"

    # Declining returns to Q&A and the offer is not repeated.
    await harness.say(reply_id=copy.CONFIRM_NO)
    assert await harness.state() == "COURSE_QNA"

    for question in ("what about projects?", "and eligibility?", "and the mode?"):
        replies = await harness.say(question)
        assert len(replies) == 1


async def test_full_pre_sales_lead_capture(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}data-science")
    await harness.say("I want a counselor to call me")

    assert await harness.state() == "ASK_NAME"

    await harness.say("Rahul Verma")
    assert await harness.state() == "ASK_CALLBACK_TIME"

    replies = await harness.say("tomorrow at 4 pm")
    assert await harness.state() == "ASK_REMARKS"
    assert "Anything specific" in replies[0].text

    replies = await harness.say("I want to know about the placement support")

    leads = await harness.leads()
    assert len(leads) == 1
    lead = leads[0]
    assert lead.name == "Rahul Verma"
    assert lead.type is LeadType.PRE_SALES
    assert lead.status is LeadStatus.NEW
    assert lead.interested_course == "Data Science"
    assert lead.preferred_time is not None
    assert lead.remarks == "I want to know about the placement support"
    assert "Rahul Verma" in replies[0].text

    # The thread is retired, so the next message starts fresh.
    assert await harness.state() == "CLOSED"


async def test_invalid_callback_time_is_rejected_then_accepted(
    harness: Harness,
) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    await harness.say("Ananya")

    replies = await harness.say("friday 3pm")

    assert await harness.state() == "ASK_CALLBACK_TIME"
    assert "closed" in replies[0].text.lower()
    assert "11 AM to 7 PM" in replies[0].text

    await harness.say("saturday 12 pm")
    assert await harness.state() == "ASK_REMARKS"


async def test_name_is_extracted_from_a_sentence(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)

    await harness.say("my name is Sneha Iyer")

    assert await harness.state() == "ASK_CALLBACK_TIME"

    async with harness.database.session() as session:
        from app.repositories.user_repository import UserRepository

        user = await UserRepository(session).get_by_phone(harness.phone)
        assert user is not None
        assert user.name == "Sneha Iyer"


async def test_unusable_name_is_asked_again(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)

    replies = await harness.say("what are the fees?")

    assert await harness.state() == "ASK_NAME"
    assert "didn't catch that" in replies[0].text


async def test_skipping_remarks_creates_a_lead_without_them(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    await harness.say("Karan")
    await harness.say("tomorrow 5pm")

    await harness.say("skip")

    leads = await harness.leads()
    assert len(leads) == 1
    assert leads[0].remarks is None


# --------------------------------------------------------------------------- #
# Post-sales
# --------------------------------------------------------------------------- #
async def test_enrolled_user_reaches_the_support_menu(harness: Harness) -> None:
    await harness.say("hi")

    replies = await harness.say(reply_id=copy.MENU_ENROLLED)

    titles = {title for _, title in replies[-1].options}
    assert {"Assignment", "Technical Issue", "Certificate"} <= titles
    assert await harness.state() == "POST_SALES"


async def test_full_post_sales_lead_capture(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_ENROLLED)
    await harness.say(reply_id=f"{copy.SUPPORT_PREFIX}technical")

    replies = await harness.say("I cannot log in to the student portal")
    assert await harness.state() == "SUPPORT_CALLBACK"
    assert {oid for oid, _ in replies[-1].options} == {copy.CONFIRM_YES, copy.CONFIRM_NO}

    await harness.say(reply_id=copy.CONFIRM_NO)  # "Please call me"
    assert await harness.state() == "ASK_NAME"

    await harness.say("Meera")
    await harness.say("tomorrow 11:30 am")
    await harness.say("skip")

    leads = await harness.leads()
    assert len(leads) == 1
    assert leads[0].type is LeadType.POST_SALES
    assert leads[0].interested_course == "Technical Issue"


async def test_support_answer_uses_post_sales_knowledge_only(
    harness: Harness,
) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_ENROLLED)
    await harness.say(reply_id=f"{copy.SUPPORT_PREFIX}assignment")

    await harness.say("where do I submit my assignment?")

    prompt = harness.llm.calls[-1]["user_prompt"]
    assert "student portal" in prompt.lower()
    # Pre-sales-only content must not leak into a support answer.
    assert "demo class before joining" not in prompt.lower()


# --------------------------------------------------------------------------- #
# Global commands and robustness
# --------------------------------------------------------------------------- #
async def test_menu_command_resets_the_flow(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}sql")

    replies = await harness.say("menu")

    assert await harness.state() == "MAIN_MENU"
    assert len(replies[0].options) == 4


async def test_asking_for_a_human_escalates_from_anywhere(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}python")

    await harness.say("can I talk to a counselor please")

    assert await harness.state() == "ASK_NAME"


async def test_menu_command_is_ignored_while_capturing_a_name(
    harness: Harness,
) -> None:
    """A form in progress must not be derailed by a stray keyword."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)

    await harness.say("Menaka")

    assert await harness.state() == "ASK_CALLBACK_TIME"


async def test_question_typed_at_the_menu_is_answered(harness: Harness) -> None:
    harness.llm.reply = "Classes are live and instructor-led."

    replies = await harness.say("are the classes live or recorded?")

    assert "Classes are live" in harness.texts(replies)
    assert harness.llm.calls


@pytest.mark.parametrize(
    "error",
    [
        LLMError("model outage"),
        # A missing GROQ_API_KEY. Regression: this used to escape the answer
        # service, roll the turn back and leave the user with no reply at all.
        ConfigurationError("GROQ_API_KEY is not set"),
        # Anything unexpected from the vendor SDK.
        RuntimeError("surprise from the SDK"),
    ],
    ids=["outage", "misconfigured", "unexpected"],
)
async def test_any_llm_failure_still_answers_and_keeps_state(
    harness: Harness, error: Exception
) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}python")

    harness.llm.fail = True
    harness.llm.error = error
    replies = await harness.say("what is the syllabus?")

    assert replies, "the user must never be left without a reply"
    assert "counselor" in harness.texts(replies).lower()
    # The flow survives: still in Q&A, and escalation remains reachable.
    assert await harness.state() == "COURSE_QNA"

    harness.llm.fail = False
    await harness.say("can I talk to a counselor")
    assert await harness.state() == "ASK_NAME"


async def test_unsupported_media_gets_a_gentle_nudge(harness: Harness) -> None:
    from app.domain.enums import MessageKind
    from app.domain.messaging import InboundMessage

    await harness.say("hi")
    inbound = InboundMessage(
        wa_message_id="wamid.sticker",
        from_phone=harness.phone,
        kind=MessageKind.UNSUPPORTED,
        text="",
    )

    before = len(harness.messaging.sent)
    await harness.service.process_inbound(inbound)
    sent = [message for _, message in harness.messaging.sent[before:]]

    assert "only read text messages" in sent[0].text


# --------------------------------------------------------------------------- #
# Lead sync
# --------------------------------------------------------------------------- #
async def test_lead_is_pushed_to_the_sink(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    await harness.say("Dev")
    await harness.say("tomorrow 3pm")
    await harness.say("skip")

    assert len(harness.sink.pushed) == 1
    record = harness.sink.pushed[0]
    assert record.name == "Dev"
    assert record.phone == harness.phone
    assert record.status == "NEW"
    assert len(record.as_row()) == 8

    leads = await harness.leads()
    assert leads[0].sync_status is SyncStatus.SYNCED


async def test_sink_failure_is_recorded_but_does_not_lose_the_lead(
    harness: Harness,
) -> None:
    harness.sink._fail = True

    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    await harness.say("Ishaan")
    await harness.say("tomorrow 3pm")
    replies = await harness.say("skip")

    leads = await harness.leads()
    assert len(leads) == 1
    assert leads[0].sync_status is SyncStatus.FAILED
    assert leads[0].sync_error
    # The user still gets a clean confirmation.
    assert "Ishaan" in replies[0].text
