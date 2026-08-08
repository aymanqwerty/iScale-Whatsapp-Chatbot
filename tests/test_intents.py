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
             "sounds good", "of course", "do it", "go ahead"]
)
def test_affirmative(text: str) -> None:
    assert intents.is_affirmative(text)


@pytest.mark.parametrize(
    "text",
    [
        # Observed in production: each of these was read as "yes" and pushed the
        # user straight into the callback form instead of answering them.
        "i want to know about courses",
        "i want to know the fees",
        "i would like to see the syllabus",
        "id like to know the duration",
    ],
)
def test_a_question_starting_with_i_want_is_not_a_yes(text: str) -> None:
    assert not intents.is_affirmative(text)


def test_call_me_is_a_human_request_not_a_yes() -> None:
    """It belongs to `wants_human`; the callback handler checks both."""
    assert intents.wants_human("call me")
    assert not intents.is_affirmative("call me")


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


# --------------------------------------------------------------------------- #
# Generalisation - these phrasings were never in the reported transcript
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    ["hello", "hey there", "namaste", "namaskar", "hola", "good morning",
     "good evening", "gm", "salaam", "kaise ho", "hii there", "yo"],
)
def test_greetings_beyond_the_reported_examples(text: str) -> None:
    assert intents.is_greeting(text)


@pytest.mark.parametrize(
    "text",
    [
        "what courses do you have",
        "which programs are there",
        "share the course list",
        "do you have any courses",
        "send me the list of courses",
        "what all do you teach",
        "what can i learn here",
        "show me what you offer",
        "i want to see your programs",
        "what trainings do you provide",
        # Hinglish - a large share of real traffic, and missed entirely by the
        # first phrase-list implementation.
        "kya kya courses hain",
        "course batao",
        "aapke paas kaunse courses hain",
    ],
)
def test_catalogue_requests_generalise(text: str) -> None:
    assert intents.wants_course_list(text)


@pytest.mark.parametrize(
    "text",
    [
        # Questions ABOUT a course must reach the model with that course's
        # knowledge, not be swallowed by the menu.
        "how long is the course",
        "what does the course cost",
        "is the course online",
        "what is the fee for data science",
        "what is the syllabus",
        "do you have weekend batches",
        "when does the batch start",
        "can i get a certificate",
        # A greeting followed by a real question is a question.
        "hi what are the fees",
        "good morning what is the price",
    ],
)
def test_course_questions_are_not_catalogue_requests(text: str) -> None:
    assert not intents.wants_course_list(text)


def test_verb_only_asks_defer_to_a_selected_course() -> None:
    """"What will I learn" means different things depending on where you are.

    With a course chosen it is a question about that course; with none chosen
    it is a request for the catalogue.
    """
    assert intents.wants_course_list("what will i learn", course_selected=False)
    assert not intents.wants_course_list("what will i learn", course_selected=True)

    # Naming the noun is unambiguous either way.
    assert intents.wants_course_list("what courses do you have", course_selected=True)
