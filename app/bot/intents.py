"""Deterministic intent detection.

Yes/no, "show me the menu" and "get me a human" are decided by rules, not by the
LLM. They are cheap, unambiguous, and must work even when the model is down -
a user asking for a counselor should never be blocked by a model outage.

Matching also accepts typed replies ("1", "explore courses") as well as tapped
buttons, because plenty of users type instead of tapping.
"""

from __future__ import annotations

import re

from app.domain.messaging import InboundMessage

_AFFIRMATIVE = frozenset(
    """
    yes y yeah yep yup yess sure ok okay k fine please pls definitely absolutely
    certainly alright right correct haan han haa ha ji jee thik theek accha
    """.split()
)

_AFFIRMATIVE_PHRASES = (
    "yes please", "go ahead", "sounds good", "that works", "why not", "of course",
    "call me", "id like", "i would like", "i want", "please do", "lets do",
    "let's do", "sure thing",
)

_NEGATIVE = frozenset(
    """
    no n nope nah na nahi later skip stop cancel not
    """.split()
)

_NEGATIVE_PHRASES = (
    "no thanks", "not now", "not right now", "maybe later", "some other time",
    "not interested", "no need", "dont call", "don't call", "do not call",
    "i'm good", "im good", "not yet",
)

_HUMAN_PHRASES = (
    "counselor", "counsellor", "human", "real person", "talk to someone",
    "speak to someone", "call me", "callback", "call back", "agent",
    "representative", "advisor", "adviser", "contact me", "phone me",
    "arrange a call", "schedule a call", "book a call",
)

_MENU_WORDS = frozenset(
    """
    menu options home back restart reset start begin main
    """.split()
)

_MENU_PHRASES = ("main menu", "go back", "start over", "start again", "show options")

_GREETINGS = frozenset(
    """
    hi hii hiii hello helo hey heyy hlo start namaste namaskar salaam hola yo
    greetings sup
    """.split()
)

_SKIP_WORDS = frozenset(
    """
    skip none nothing no nope na nil nah nothanks
    """.split()
)

_SKIP_PHRASES = ("no thanks", "nothing else", "no remarks", "not really", "that's all",
                 "thats all", "no note")

_PUNCT_RE = re.compile(r"[^\w\s']")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    return _PUNCT_RE.sub(" ", text.lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(normalize(text).split())


def is_affirmative(text: str) -> bool:
    cleaned = normalize(text)
    if not cleaned:
        return False
    if any(phrase in cleaned for phrase in _AFFIRMATIVE_PHRASES):
        return True
    words = cleaned.split()
    # Only treat a short utterance as a bare "yes"; "yes but what about the fees"
    # is a question, not a confirmation.
    if len(words) <= 3 and any(word in _AFFIRMATIVE for word in words):
        return not (_tokens(cleaned) & _NEGATIVE)
    return False


def is_negative(text: str) -> bool:
    cleaned = normalize(text)
    if not cleaned:
        return False
    if any(phrase in cleaned for phrase in _NEGATIVE_PHRASES):
        return True
    words = cleaned.split()
    return len(words) <= 3 and any(word in _NEGATIVE for word in words)


def wants_human(text: str) -> bool:
    cleaned = normalize(text)
    return any(phrase in cleaned for phrase in _HUMAN_PHRASES)


def wants_menu(text: str) -> bool:
    cleaned = normalize(text)
    if any(phrase in cleaned for phrase in _MENU_PHRASES):
        return True
    words = cleaned.split()
    # A single word only - "back to the menu of topics you offer" is a question.
    return len(words) == 1 and words[0] in _MENU_WORDS


def is_greeting(text: str) -> bool:
    words = normalize(text).split()
    return bool(words) and len(words) <= 2 and words[0] in _GREETINGS


def is_skip(text: str) -> bool:
    cleaned = normalize(text)
    if not cleaned or cleaned in {"-", "_"}:
        return True
    if any(phrase in cleaned for phrase in _SKIP_PHRASES):
        return True
    words = cleaned.split()
    return len(words) <= 2 and all(word in _SKIP_WORDS for word in words)


# --------------------------------------------------------------------------- #
# Option matching
# --------------------------------------------------------------------------- #
def match_option(
    inbound: InboundMessage,
    options: tuple[tuple[str, str, str, tuple[str, ...]], ...],
) -> str | None:
    """Resolve an inbound message to one of `options`.

    Tried in order of confidence:
      1. the id sent back by a tapped button or list row,
      2. an exact title match ("Explore Courses"),
      3. a positional number ("2"),
      4. a keyword hit ("i want to see the courses").
    """
    ids = {oid for oid, _, _, _ in options}
    if inbound.reply_id and inbound.reply_id in ids:
        return inbound.reply_id

    cleaned = normalize(inbound.text)
    if not cleaned:
        return None

    for oid, title, _, _ in options:
        if cleaned == normalize(title):
            return oid

    if cleaned.isdigit():
        index = int(cleaned) - 1
        if 0 <= index < len(options):
            return options[index][0]

    # Longest keyword first so "data science" beats "data".
    best: tuple[int, str] | None = None
    for oid, _, _, keywords in options:
        for keyword in keywords:
            if re.search(rf"\b{re.escape(keyword)}\b", cleaned) and (
                best is None or len(keyword) > best[0]
            ):
                best = (len(keyword), oid)
    return best[1] if best else None
