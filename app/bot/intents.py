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

#: Only unambiguous confirmations belong here, because a false positive turns a
#: question into a booking. "i want", "id like" and "call me" were removed after
#: "i want to know about courses" and "i want to know the fees" were both read
#: as "yes" and pushed the user straight into the callback form. A request to
#: speak to someone is `wants_human`'s job, not this one.
_AFFIRMATIVE_PHRASES = (
    "yes please", "yes do", "go ahead", "sounds good", "that works", "why not",
    "of course", "please do", "lets do", "let's do", "sure thing", "do it",
    "that would be great", "that'd be great",
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

#: Matched as substrings, so every entry here must be safe inside a longer
#: word. "agent" is NOT - it lives in "agentic", and we sell a course called
#: Machine Learning with Agentic AI, so asking about it threw the user straight
#: into callback capture. Word-boundary triggers go in `_HUMAN_WORDS` instead.
_HUMAN_PHRASES = (
    "counselor", "counsellor", "real person", "talk to someone",
    "speak to someone", "call me", "callback", "call back",
    "representative", "contact me", "phone me",
    "arrange a call", "schedule a call", "book a call",
)

#: Single words that must match whole - "human" must not fire on "humanities",
#: "advisor" not on "advisory". "agent" is absent deliberately: we teach AI
#: agents in both cohort courses, so "does it cover ai agents?" is a syllabus
#: question, not a request for a person.
_HUMAN_WORDS = re.compile(r"\b(?:human|advisor|adviser|executive)\b")

#: "agent" only counts when someone is asking to be put through to one. This is
#: the difference between "connect me to an agent" and "does it cover agents".
_HUMAN_AGENT_RE = re.compile(
    r"\b(?:talk|speak|connect|transfer|chat|put\s+me|route\s+me)\b"
    r"[\w\s']{0,20}?\bagents?\b"
)

#: A request verb followed shortly by a "call" word: "i want a call",
#: "need a callback for data science", "can you schedule a cal on sunday".
#:
#: The fixed phrase list above misses these, and missing one is expensive: the
#: turn falls through to the LLM, which answers conversationally instead of
#: starting the booking flow - so no lead is created. `cal` is accepted because
#: it is a common typo and the cost of missing an escalation far exceeds the
#: cost of an occasional false positive (which only offers a callback).
_ASK_VERB = (
    r"(?:want|wants|wanna|need|needs|like|get|give|arrange|schedule|"
    r"book|fix|set\s?up|request|require)"
)
_CALL_WORD = r"(?:call\s?back|callback|calls|call|cal)"
_HUMAN_RE = re.compile(rf"\b{_ASK_VERB}\b[\w\s']{{0,24}}?\b{_CALL_WORD}\b")

#: Asking what is on offer. Answered with the course menu, never with prose:
#: the model once replied "that's the only course I have information on" while
#: eight courses were loaded, because it sees retrieved snippets, not the
#: catalogue.
#:
#: Composed from three signals rather than a list of exact phrases. A fixed
#: phrase list looked fine against the examples it was written from and then
#: missed "what all do you teach", "do you have any courses" and every Hinglish
#: form - which is most of the real traffic.
_CATALOGUE_NOUNS = frozenset(
    """
    course courses program programs programme programmes training trainings
    class classes syllabus curriculum
    """.split()
)

#: Words that turn a noun into "show me the list".
_CATALOGUE_ASKS = frozenset(
    """
    what which list show tell share send see explore know have has offer offers
    offered teach teaches taught provide provides available availabe all every
    each any options option about kind kinds type types
    kya kaun kaunsa konsa konse batao dikhao bata hai hain
    """.split()
)

#: Asking ABOUT a course, not asking WHICH courses exist. These win, because
#: "how long is the course" must reach the model with the course's own snippets.
_COURSE_ATTRIBUTE_WORDS = frozenset(
    """
    fee fees cost costs price pricing charge charges emi installment instalment
    discount scholarship refund duration long month months week weeks
    eligibility eligible prerequisite requirement syllabus module modules topic
    topics project projects certificate certification placement job salary
    batch batches timing timings schedule demo trial start starts begin
    online offline mode language recorded live
    """.split()
)

#: Moving an existing booking rather than making a new one. Checked before
#: `wants_human`, which would otherwise start a fresh capture and leave the
#: original call still on the counselor's list.
_RESCHEDULE_PHRASES = (
    "reschedule", "re schedule", "resched", "change my call", "change the call",
    "change my appointment", "move my call", "move the call", "shift my call",
    "shift the call", "postpone", "prepone", "another time", "different time",
    "change the time", "change my time", "change timing", "change my slot",
    "cancel and rebook", "call me later instead", "not that time",
    "samay badal", "time change kar", "call aage badha",
)

_MENU_WORDS = frozenset(
    """
    menu options home back restart reset start begin main
    """.split()
)

_MENU_PHRASES = ("main menu", "go back", "start over", "start again", "show options")

_GREETINGS = frozenset(
    """
    hi hii hiii hiiii hello helo hellow hey heyy heyyy hlo hyy start namaste
    namaskar namaskaar salaam salam assalam hola yo greetings sup gm gn
    good morning afternoon evening
    """.split()
)

#: Two-word openers, checked before the single-word list so "good morning"
#: is not mistaken for a question that happens to start with "good".
_GREETING_PHRASES = (
    "good morning", "good afternoon", "good evening", "good day",
    "how are you", "kaise ho", "kya haal",
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
    if any(phrase in cleaned for phrase in _HUMAN_PHRASES):
        return True
    if _HUMAN_WORDS.search(cleaned) or _HUMAN_AGENT_RE.search(cleaned):
        return True
    return bool(_HUMAN_RE.search(cleaned))


def wants_reschedule(text: str) -> bool:
    """True when the user wants to move a call they have already booked."""
    cleaned = normalize(text)
    return any(phrase in cleaned for phrase in _RESCHEDULE_PHRASES)


def wants_menu(text: str) -> bool:
    cleaned = normalize(text)
    if any(phrase in cleaned for phrase in _MENU_PHRASES):
        return True
    words = cleaned.split()
    # A single word only - "back to the menu of topics you offer" is a question.
    return len(words) == 1 and words[0] in _MENU_WORDS


#: "what do you teach", "what can i learn", "show me what you offer" - asking
#: for the catalogue without naming it. Only trusted when no course is already
#: selected, because "what will I learn" inside a chosen course is a question
#: about that course, not a request for the list.
_TEACHING_VERBS = frozenset(
    "teach teaches taught learn learns offer offers provide provides".split()
)


def wants_course_list(text: str, *, course_selected: bool = False) -> bool:
    """True when the user is asking what is on offer.

    Requires a catalogue noun ("courses", "programs") AND a listing word
    ("what", "show", "kya", "batao"), and refuses when the message names a
    course attribute - because "how long is the course" and "what does the
    course cost" are questions about a course, not requests for the list, and
    must reach the model with that course's own knowledge.

    `course_selected` narrows the looser verb-only forms, which would otherwise
    hijack "what will I learn" once someone is deep in a single course.
    """
    words = _tokens(text)
    if words & _COURSE_ATTRIBUTE_WORDS:
        return False
    if not words & _CATALOGUE_ASKS:
        return False
    if words & _CATALOGUE_NOUNS:
        return True
    return not course_selected and bool(words & _TEACHING_VERBS)


def is_greeting(text: str) -> bool:
    """True for an opener like "hi" or "hello there".

    Kept to short utterances on purpose. "hi, what are the fees for data
    science" is a question that happens to start politely, and answering it
    with the menu would be worse than answering the question.
    """
    cleaned = normalize(text)
    words = cleaned.split()
    if not words:
        return False
    # "good morning" / "kaise ho" - only when that is the whole message, so
    # "good morning, what are the fees" still reaches the model as a question.
    if any(cleaned.startswith(phrase) for phrase in _GREETING_PHRASES):
        return len(words) <= 4
    if words[0] not in _GREETINGS:
        return False
    # Allow "hi there", "hello sir" - but not a greeting with a question after it.
    return len(words) <= 3


#: Ways people say they have not decided. Matched as substrings of the
#: normalised text so "i'm not really sure tbh" lands as readily as "not sure".
_UNDECIDED_PHRASES = (
    "not sure", "no idea", "not decided", "havent decided", "have not decided",
    "dont know", "do not know", "dunno", "confused", "help me choose",
    "help me decide", "suggest me", "suggest something", "you decide",
    "which one is better for me", "kuch bhi", "pata nahi", "samajh nahi",
)


#: Ways people refuse to hand over a detail. Separate from `is_negative`, which
#: answers yes/no questions - "no thanks" declines an offer, whereas these
#: decline to *share something*, which is the distinction that matters when a
#: booking is gated on it.
_DECLINE_PHRASES = (
    "rather not", "would not like to", "wouldnt like to", "dont want to",
    "do not want to", "not comfortable", "cant share", "cannot share",
    "wont share", "will not share", "dont have", "do not have",
    "not sharing", "why do you need", "is it necessary", "prefer not",
)


def declines_to_share(text: str) -> bool:
    """Whether the user is refusing to give a detail we asked for."""
    cleaned = normalize(text)
    if not cleaned:
        return False
    if any(phrase in cleaned for phrase in _DECLINE_PHRASES):
        return True
    # A leading "no" is a refusal even when the rest is a polite sentence.
    return cleaned.split()[0] in {"no", "nope", "nah"}


#: Explicit buying signals. Narrow on purpose: this short-circuits the usual
#: "earn it over a few messages" pacing and shows the discount immediately, so a
#: false positive means dangling a coupon at someone who was only browsing.
_ENROL_PHRASES = (
    "how to join", "how do i join", "how can i join", "want to join",
    "how to enroll", "how do i enroll", "how to enrol", "want to enroll",
    "how to buy", "how do i buy", "want to buy", "how to purchase",
    "how to pay", "how do i pay", "want to pay", "payment link",
    "how to register", "want to register", "sign me up", "count me in",
    "i want this course", "i want this program", "ready to join",
    "kaise join", "kaise le", "kaise kharide",
)


def wants_to_enroll(text: str) -> bool:
    """Whether the user is asking how to actually buy."""
    cleaned = normalize(text)
    if not cleaned:
        return False
    return any(phrase in cleaned for phrase in _ENROL_PHRASES)


def means_undecided(text: str) -> bool:
    """Whether the user is saying they do not know which course they want.

    Deliberately narrow. This routes into the discovery pitch, so a false
    positive hijacks someone who actually asked a specific question - which is
    why "which one" alone is not enough, but "which one is better for me" is.
    """
    cleaned = normalize(text)
    if not cleaned:
        return False
    return any(phrase in cleaned for phrase in _UNDECIDED_PHRASES)


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
