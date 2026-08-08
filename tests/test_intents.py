"""Deterministic intent detection and option matching."""

from __future__ import annotations

import pytest

from app.bot import copy, intents
from app.bot.handlers.common import clean_name
from app.domain.enums import MessageKind
from app.domain.messaging import InboundMessage


def _inbound(text: str = "", reply_id: str | None = None) -> InboundMessage:
    return InboundMessage(
        wa_message_id="wamid.test",
        from_phone="919999999999",
        kind=MessageKind.INTERACTIVE if reply_id else MessageKind.TEXT,
        text=text,
        reply_id=reply_id,
    )


@pytest.mark.parametrize(
    "text", ["yes", "Yes!", "yeah", "sure", "ok", "haan", "yes please", "please do",
             "sounds good", "of course", "call me"]
)
def test_affirmative(text: str) -> None:
    assert intents.is_affirmative(text)


@pytest.mark.parametrize(
    "text", ["no", "nope", "not now", "no thanks", "maybe later", "not interested"]
)
def test_negative(text: str) -> None:
    assert intents.is_negative(text)


def test_a_question_containing_yes_is_not_a_confirmation() -> None:
    """"yes but…" is a follow-up question, not consent to a callback."""
    assert not intents.is_affirmative("yes but what about the fees and the batches")


@pytest.mark.parametrize(
    "text",
    ["talk to a counselor", "I want a callback", "can I speak to someone",
     "connect me to a human", "please call back"],
)
def test_wants_human(text: str) -> None:
    assert intents.wants_human(text)


def test_menu_needs_a_bare_keyword() -> None:
    assert intents.wants_menu("menu")
    assert intents.wants_menu("main menu")
    # A sentence that merely mentions the word is a question, not a command.
    assert not intents.wants_menu("do you have a menu of all the courses you offer")


@pytest.mark.parametrize("text", ["hi", "Hello", "hey there", "namaste"])
def test_greeting(text: str) -> None:
    assert intents.is_greeting(text)


def test_long_message_starting_with_hi_is_not_just_a_greeting() -> None:
    assert not intents.is_greeting("hi I want to know about the data science course")


@pytest.mark.parametrize("text", ["skip", "none", "nothing", "no", "-", ""])
def test_skip(text: str) -> None:
    assert intents.is_skip(text)


def test_real_remark_is_not_a_skip() -> None:
    assert not intents.is_skip("please explain the placement process in detail")


# --------------------------------------------------------------------------- #
# Option matching
# --------------------------------------------------------------------------- #
def test_tapped_reply_id_wins() -> None:
    inbound = _inbound(text="anything", reply_id=copy.MENU_COURSES)

    assert intents.match_option(inbound, copy.MAIN_MENU_OPTIONS) == copy.MENU_COURSES


def test_exact_title_match() -> None:
    assert (
        intents.match_option(_inbound("Explore Courses"), copy.MAIN_MENU_OPTIONS)
        == copy.MENU_COURSES
    )


def test_positional_number_match() -> None:
    assert (
        intents.match_option(_inbound("3"), copy.MAIN_MENU_OPTIONS)
        == copy.MENU_COUNSELOR
    )


def test_keyword_match() -> None:
    assert (
        intents.match_option(
            _inbound("I'm already a student here"), copy.MAIN_MENU_OPTIONS
        )
        == copy.MENU_ENROLLED
    )


def test_no_match_returns_none() -> None:
    assert intents.match_option(_inbound("blah blah"), copy.MAIN_MENU_OPTIONS) is None


# --------------------------------------------------------------------------- #
# Name extraction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Rahul", "Rahul"),
        ("rahul verma", "Rahul Verma"),
        ("My name is Priya Sharma", "Priya Sharma"),
        ("I am Aarav", "Aarav"),
        ("this is Neha.", "Neha"),
        ("D'Souza", "D'Souza"),
    ],
)
def test_clean_name_accepts(raw: str, expected: str) -> None:
    assert clean_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "?", "what are the fees?", "1234", "call me at 5pm",
     "a" * 80],
)
def test_clean_name_rejects(raw: str) -> None:
    assert clean_name(raw) is None


# --------------------------------------------------------------------------- #
# Callback requests phrased freely
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        # Observed in production: none of these hit the fixed phrase list, so
        # the turn fell through to the LLM, which answered conversationally and
        # claimed to have booked a call. No lead was ever created.
        "i want a call for data science on sunday 4 pm",
        "i want to schedule a cal",  # typo: the user dropped an l
        "need a call regarding fees",
        "can you arrange a callback",
        "give me a call tomorrow",
        "book a call for me",
        "i wanna call back",
        "please set up a call",
    ],
)
def test_free_form_callback_requests_escalate(text: str) -> None:
    assert intents.wants_human(text)


@pytest.mark.parametrize(
    "text",
    [
        "what is the fee for data science",
        "how long is the course",
        "i want to know about the syllabus",
        "what tools are called in the course",
        "i need to recall my password",
        "do you provide placement",
    ],
)
def test_ordinary_questions_do_not_escalate(text: str) -> None:
    """A false positive only offers a callback, but it derails a real question."""
    assert not intents.wants_human(text)
