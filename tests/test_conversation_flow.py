"""End-to-end conversation flows through the real state machine."""

from __future__ import annotations

import pytest

from app.bot import copy
from app.core.exceptions import ConfigurationError, LLMError
from app.domain.enums import LeadStatus, LeadType, SyncStatus
from app.services.crm.base import LEAD_COLUMNS
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
    }
    titles = {title for _, title in replies[0].options}
    assert titles == {"Not Enrolled Yet", "Already Enrolled", "Talk to a Counselor"}
    assert await harness.state() == "MAIN_MENU"


async def test_retired_general_option_is_still_handled(harness: Harness) -> None:
    """Old menu messages stay tappable in WhatsApp indefinitely.

    "General Question" was removed from the menu, but a user scrolling back
    through the thread can still tap it. Dropping the branch would answer them
    with a fallback instead of the thing they asked for.
    """
    await harness.say("hi")

    await harness.say(reply_id=copy.MENU_GENERAL)

    assert await harness.state() == "GENERAL_QNA"


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
    assert "Master Of Data Analytics Program" in titles
    assert "AI Engineer Advance Program" in titles
    # Non-featured courses are behind the "Other courses" row, not listed.
    assert "Free Data Analytics Course" not in titles
    assert "Not sure yet" in titles
    assert await harness.state() == "COURSE_SELECTION"


async def test_selecting_a_course_enters_qna(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)

    replies = await harness.say(reply_id=f"{copy.COURSE_PREFIX}data-science-with-generative-ai")

    assert "Data Science With Generative AI Course" in replies[0].text
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
    harness.llm.reply = "The Master Of Data Analytics Program runs for 3 to 6 months."
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}master-of-data-analytics")

    replies = await harness.say("how long is the course?")

    assert replies[0].text == "The Master Of Data Analytics Program runs for 3 to 6 months."
    prompt = harness.llm.calls[-1]["user_prompt"]
    assert "KNOWLEDGE" in prompt
    assert "3 months" in prompt  # the duration snippet was actually retrieved


async def test_callback_is_offered_after_the_nudge_threshold(
    harness: Harness,
) -> None:
    """Three answered questions, then one offer - and only one."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}master-of-data-analytics")

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
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}data-science-with-generative-ai")
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
    assert lead.interested_course == "Data Science With Generative AI Course"
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


async def test_support_answer_is_grounded_and_audience_filtered(
    harness: Harness,
) -> None:
    """A support answer must still be built from retrieved knowledge.

    This deliberately asserts the mechanism rather than any particular phrase:
    the FAQ file is business-owned content that gets rewritten, and pinning the
    test to a sentence in it makes every content edit look like a code failure.
    Audience filtering itself is covered in `test_knowledge.py`.
    """
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_ENROLLED)
    await harness.say(reply_id=f"{copy.SUPPORT_PREFIX}assignment")

    await harness.say("where do I submit my assignment?")

    call = harness.llm.calls[-1]
    assert "KNOWLEDGE" in call["user_prompt"]
    assert "SUPPORT MODE" in call["system_prompt"]


# --------------------------------------------------------------------------- #
# Global commands and robustness
# --------------------------------------------------------------------------- #
async def test_menu_command_resets_the_flow(harness: Harness) -> None:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}sql")

    replies = await harness.say("menu")

    assert await harness.state() == "MAIN_MENU"
    assert len(replies[0].options) == len(copy.MAIN_MENU_OPTIONS)


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
    await harness.say(reply_id=f"{copy.COURSE_PREFIX}ai-engineer-advance-program")

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
    assert len(record.as_row()) == len(LEAD_COLUMNS)

    # The callback slot must reach the sheet as separate date and time values,
    # not one sentence: a counselor filters "Callback Date = today" to find who
    # to ring, which inert text cannot answer.
    assert record.callback_date == "2026-08-06"  # frozen clock: Wed 5 Aug + 1
    assert record.callback_time == "15:00"
    assert record.callback_raw == "tomorrow 3pm"

    row = record.as_row()
    assert row[LEAD_COLUMNS.index("Callback Date")] == "2026-08-06"
    assert row[LEAD_COLUMNS.index("Callback Time")] == "15:00"

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


async def test_leads_captured_before_the_sink_existed_are_backfilled(
    harness: Harness,
) -> None:
    """SKIPPED must not be a dead end.

    Every lead taken while Google Sheets was switched off carries SKIPPED - the
    normal state during development. Those rows were invisible to
    `retry_pending`, so turning the sheet on later left them stranded in
    PostgreSQL with no way to push them through short of editing the database.
    """
    from app.services.crm.null_sink import NullLeadSink
    from app.services.lead_service import LeadSyncService

    # Capture a lead with no sink configured, exactly as the live bot did.
    disabled = LeadSyncService(harness.database, NullLeadSink(), harness.service._settings)
    harness.service._lead_sync = disabled

    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    await harness.say("Nishant")
    await harness.say("tomorrow 3pm")
    await harness.say("skip")

    leads = await harness.leads()
    assert len(leads) == 1
    assert leads[0].sync_status is SyncStatus.SKIPPED
    assert harness.sink.pushed == []

    # Now the sheet is configured - the operator retries.
    enabled = LeadSyncService(harness.database, harness.sink, harness.service._settings)
    synced = await enabled.retry_pending()

    assert synced == 1
    assert [record.name for record in harness.sink.pushed] == ["Nishant"]
    leads = await harness.leads()
    assert leads[0].sync_status is SyncStatus.SYNCED


# --------------------------------------------------------------------------- #
# Consistency: deterministic intents beat the model
# --------------------------------------------------------------------------- #
async def test_a_greeting_always_returns_the_main_menu(harness: Harness) -> None:
    """Observed: "hi" mid-conversation got improvised prose and no menu."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say("what are your working hours")

    replies = await harness.say("hi")

    assert await harness.state() == "MAIN_MENU"
    assert {t for _, t in replies[0].options} == {
        "Not Enrolled Yet",
        "Already Enrolled",
        "Talk to a Counselor",
    }


async def test_a_tapped_menu_row_works_from_any_state(harness: Harness) -> None:
    """WhatsApp rows stay tappable forever.

    Observed: tapping "Not Enrolled Yet" during Q&A sent the row's title to the
    model, which answered a question about class recordings.
    """
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)
    await harness.say("what are your working hours")
    assert await harness.state() == "GENERAL_QNA"

    replies = await harness.say(reply_id=copy.MENU_COURSES)

    assert await harness.state() == "COURSE_SELECTION"
    assert any("programs" in r.text for r in replies)


async def test_asking_what_courses_exist_shows_the_menu(harness: Harness) -> None:
    """Observed: the model replied "that's the only course I have information on".

    It sees retrieved snippets, not the catalogue, so it under-reports. The menu
    is built from courses.json and is always complete.
    """
    await harness.say("hi")

    replies = await harness.say("tell me about each and every course available")

    assert await harness.state() == "COURSE_SELECTION"
    titles = {t for _, t in replies[0].options}
    assert "AI Engineer Advance Program" in titles
    assert harness.llm.calls == [], "the model must not be consulted for this"


async def test_a_question_is_not_treated_as_a_booking_confirmation(
    harness: Harness,
) -> None:
    """Observed: "i want to know about courses" was read as "yes" and asked for a name."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COURSES)

    await harness.say("i want to know about courses")

    assert await harness.state() != "ASK_NAME"


# --------------------------------------------------------------------------- #
# Booking: name and date given together
# --------------------------------------------------------------------------- #
async def test_a_name_given_while_answering_the_time_is_kept(
    harness: Harness,
) -> None:
    """Observed: the lead went out under the previous name."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    await harness.say("Akshat Trivedi")

    await harness.say("my name is Ayush Raj and schedule the call for 11 august at 4 pm")
    await harness.say("skip")

    leads = await harness.leads()
    assert leads[0].name == "Ayush Raj"


async def test_a_rejected_time_keeps_the_date_the_user_gave(
    harness: Harness,
) -> None:
    """Observed: "10 august at 4:30 am" was refused, and "4:30 pm then" silently
    booked today instead of the 10th. The counselor calls on the wrong day and
    the lead is wasted."""
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    await harness.say("Akshat Trivedi")

    # 4:30 am is outside calling hours, so this is rejected.
    await harness.say("schedule the call for 10 august at 4:30 am")
    # Only a time - the day must carry over from the rejected attempt.
    await harness.say("do for 4:30 pm then")
    await harness.say("skip")

    leads = await harness.leads()
    assert leads[0].preferred_time is not None
    assert leads[0].preferred_time.day == 10


# --------------------------------------------------------------------------- #
# Rescheduling
# --------------------------------------------------------------------------- #
async def _book(harness: Harness, when: str) -> int:
    await harness.say("hi")
    await harness.say(reply_id=copy.MENU_COUNSELOR)
    await harness.say("Ayman Akram")
    await harness.say(when)
    await harness.say("skip")
    leads = await harness.leads()
    return int(leads[0].id)


async def test_rescheduling_moves_the_same_lead(harness: Harness) -> None:
    """One row, its time changed - not a second booking.

    A duplicate would put the same person on the counselor's list twice with
    two different times, which is exactly the confusion rescheduling exists to
    remove.
    """
    lead_id = await _book(harness, "monday 4pm")

    await harness.say("i want to reschedule my call")
    await harness.say("tuesday 5pm")
    replies = await harness.say("skip")

    leads = await harness.leads()
    assert len(leads) == 1, "rescheduling must not create a second lead"
    assert int(leads[0].id) == lead_id
    assert leads[0].preferred_time is not None
    assert leads[0].preferred_time.day == 11
    assert "moved" in harness.texts(replies).lower()


async def test_rescheduling_marks_the_lead_for_resync(harness: Harness) -> None:
    """The sheet must be brought back in line, or it keeps showing the old time."""
    await _book(harness, "monday 4pm")

    await harness.say("i want to reschedule my call")
    await harness.say("tuesday 5pm")
    await harness.say("skip")

    leads = await harness.leads()
    assert leads[0].sync_status in (SyncStatus.PENDING, SyncStatus.SYNCED)


async def test_booking_again_offers_to_move_the_existing_call(
    harness: Harness,
) -> None:
    """Observed in testing: a user booked twice without noticing.

    Silently adding a second booking sends a counselor to ring the same person
    twice; silently replacing it loses a call someone may genuinely want. Ask.
    """
    await _book(harness, "monday 4pm")

    replies = await harness.say("i want to talk to a counsellor")

    assert {oid for oid, _ in replies[-1].options} == {
        copy.RESCHEDULE_MOVE,
        copy.RESCHEDULE_NEW,
    }
    assert "already have a call booked" in replies[-1].text


async def test_choosing_book_another_creates_a_second_lead(
    harness: Harness,
) -> None:
    """Someone who genuinely wants two calls must be able to have them."""
    await _book(harness, "monday 4pm")

    await harness.say("i want to talk to a counsellor")
    await harness.say(reply_id=copy.RESCHEDULE_NEW)
    await harness.say("wednesday 12pm")
    await harness.say("skip")

    leads = await harness.leads()
    assert len(leads) == 2


async def test_reschedule_with_nothing_booked_starts_a_booking(
    harness: Harness,
) -> None:
    """Nothing to move - take it as a request for a call rather than an error."""
    await harness.say("hi")

    replies = await harness.say("i want to reschedule my call")

    assert "can't find" in harness.texts(replies).lower()
    assert await harness.state() in ("ASK_NAME", "ASK_CALLBACK_TIME")


async def test_the_booked_time_is_shown_in_business_timezone(
    harness: Harness,
) -> None:
    """Regression: a 4 PM booking was read back to the user as 10:30 AM.

    The column stores the absolute instant, so a value from the database
    arrives in UTC. Correct to the second, and useless to the reader.
    """
    await _book(harness, "monday 4pm")

    replies = await harness.say("i want to reschedule my call")

    assert "4 PM" in harness.texts(replies)
    assert "10:30" not in harness.texts(replies)
