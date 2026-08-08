"""All user-facing text and menu definitions.

Kept in one module so the tone of the bot can be reviewed and edited by a
non-developer without touching flow logic, and so nothing is hard-coded inside a
handler.
"""

from __future__ import annotations

from app.domain.messaging import Button, ListRow, OutboundMessage
from app.services.knowledge.loader import KnowledgeBase

# --------------------------------------------------------------------------- #
# Option ids. Buttons and list rows send these back verbatim.
# --------------------------------------------------------------------------- #
MENU_COURSES = "menu:courses"
MENU_ENROLLED = "menu:enrolled"
MENU_COUNSELOR = "menu:counselor"
#: Retired from the menu but still handled. Old messages stay tappable in
#: WhatsApp forever, so a user scrolling back could still send this id - and a
#: dropped branch would answer them with "I didn't understand that".
MENU_GENERAL = "menu:general"

COURSE_PREFIX = "course:"
COURSE_UNSURE = "course:unsure"
#: Opens the second-level list of non-featured courses.
COURSE_OTHERS = "course:others"

SUPPORT_PREFIX = "support:"

RESCHEDULE_MOVE = "reschedule:move"
RESCHEDULE_NEW = "reschedule:new"

CONFIRM_YES = "confirm:yes"
CONFIRM_NO = "confirm:no"

#: (id, title, description, keywords the user might type instead of tapping)
#:
#: The description is no longer rendered - menus show titles only, which reads
#: far cleaner on a phone. It is kept here as documentation of what each option
#: means, and because the keyword matching below is easier to review beside it.
#:
#: Three options, split by where the person is rather than by what they want to
#: do. Every path ends at the same place - a call booked with a counselor - so
#: the third option is simply the shortcut for someone who already knows that.
#:
#: "General Question" was removed deliberately: free text is answered in any
#: state, so the option only ever added a decision without adding a capability.
MAIN_MENU_OPTIONS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        MENU_COURSES,
        "Not Enrolled Yet",
        "Explore our courses",
        (
            "not enrolled", "new", "course", "courses", "explore", "program",
            "programme", "learn", "join", "admission", "interested",
        ),
    ),
    (
        MENU_ENROLLED,
        "Already Enrolled",
        "Support for current students",
        ("enrolled", "student", "already", "joined", "support", "existing"),
    ),
    (
        MENU_COUNSELOR,
        "Talk to a Counselor",
        "Book a call with our team",
        ("counselor", "counsellor", "callback", "call", "talk", "human", "agent", "advisor"),
    ),
)

SUPPORT_OPTIONS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        f"{SUPPORT_PREFIX}assignment",
        "Assignment",
        "Submissions, deadlines, feedback",
        ("assignment", "homework", "submission", "project", "deadline"),
    ),
    (
        f"{SUPPORT_PREFIX}technical",
        "Technical Issue",
        "Login, portal or access problems",
        ("technical", "login", "portal", "password", "access", "error", "bug", "issue"),
    ),
    (
        f"{SUPPORT_PREFIX}placement",
        "Placement",
        "Placement support and preparation",
        ("placement", "job", "interview", "resume", "referral", "career"),
    ),
    (
        f"{SUPPORT_PREFIX}certificate",
        "Certificate",
        "Completion certificate queries",
        ("certificate", "certification", "completion"),
    ),
    (
        f"{SUPPORT_PREFIX}timing",
        "Class Timing",
        "Schedule, batch and class links",
        ("timing", "time", "schedule", "batch", "class", "link", "session"),
    ),
    (
        f"{SUPPORT_PREFIX}other",
        "Other",
        "Something else",
        ("other", "else", "different"),
    ),
)


# --------------------------------------------------------------------------- #
# Menu builders
# --------------------------------------------------------------------------- #
def welcome_message(company: str, *, returning_name: str | None = None) -> OutboundMessage:
    greeting = (
        f"Welcome back, {returning_name}! 👋" if returning_name else f"Welcome to {company}! 👋"
    )
    return OutboundMessage(
        text=f"{greeting}\n\nHow can I help you today?",
        list_rows=tuple(
            ListRow(id=oid, title=title)
            for oid, title, _, _ in MAIN_MENU_OPTIONS
        ),
        list_button_label="Choose an option",
        header="Main menu",
    )


def main_menu(prompt: str = "What would you like to do next?") -> OutboundMessage:
    return OutboundMessage(
        text=prompt,
        list_rows=tuple(
            ListRow(id=oid, title=title)
            for oid, title, _, _ in MAIN_MENU_OPTIONS
        ),
        list_button_label="Choose an option",
        header="Main menu",
    )


def course_menu(knowledge_base: KnowledgeBase) -> OutboundMessage:
    """Built from `courses.json`, so adding a course needs no code change.

    Only the flagged (`featured: true`) courses get a row. The rest sit behind
    one "Other courses" row - a short list converts better than a wall of
    options, and WhatsApp caps a list at ten rows regardless.
    """
    rows = [
        ListRow(id=f"{COURSE_PREFIX}{course.slug}", title=course.name)
        for course in knowledge_base.featured_courses
    ]
    # Reserve the last two rows for "Other courses" and "Not sure".
    rows = rows[:8]
    if knowledge_base.other_courses:
        rows.append(
            ListRow(id=COURSE_OTHERS, title="Other courses")
        )
    rows.append(
        ListRow(id=COURSE_UNSURE, title="Not sure yet")
    )
    return OutboundMessage(
        text="Here are our main programs. Which one would you like to know about?",
        list_rows=tuple(rows),
        list_button_label="View courses",
        header="Our courses",
    )


def other_courses_menu(knowledge_base: KnowledgeBase) -> OutboundMessage:
    """Second-level list of the non-featured courses."""
    rows = [
        ListRow(id=f"{COURSE_PREFIX}{course.slug}", title=course.name)
        for course in knowledge_base.other_courses
    ][:9]
    rows.append(ListRow(id=COURSE_UNSURE, title="Not sure yet"))
    return OutboundMessage(
        text="These are our shorter and free programs.",
        list_rows=tuple(rows),
        list_button_label="View courses",
        header="Other courses",
    )


def support_menu() -> OutboundMessage:
    return OutboundMessage(
        text="Sure - what do you need help with?",
        list_rows=tuple(
            ListRow(id=oid, title=title) for oid, title, _, _ in SUPPORT_OPTIONS
        ),
        list_button_label="Choose a topic",
        header="Student support",
    )


def yes_no(
    text: str, *, yes_label: str = "Yes, please", no_label: str = "Not now"
) -> OutboundMessage:
    return OutboundMessage(
        text=text,
        buttons=(
            Button(id=CONFIRM_YES, title=yes_label),
            Button(id=CONFIRM_NO, title=no_label),
        ),
    )


# --------------------------------------------------------------------------- #
# Plain messages
# --------------------------------------------------------------------------- #
COURSE_SELECTED = (
    "Great choice - {course}.\n\n{summary}\n\n"
    "Ask me anything about it: duration, syllabus, projects, eligibility or batches."
)

UNSURE_COURSE = (
    "No problem, that's a common question. Tell me a little about yourself - "
    "your background and what kind of role you're aiming for - and I'll point you "
    "to the right program."
)

GENERAL_QNA_INTRO = (
    "Of course - go ahead and ask. I can help with course comparisons, "
    "learning roadmaps, prerequisites and career questions."
)

ASK_CALLBACK_PRE_SALES = (
    "Would you like one of our counselors to call you? "
    "They can walk you through the fees, batches and anything else in detail."
)

ASK_CALLBACK_POST_SALES = (
    "Would you like our support team to call you and sort this out?"
)

ASK_NAME = "Sure! May I know your name, please?"

ASK_NAME_RETRY = (
    "Sorry, I didn't catch that. Could you share your name as you'd like the "
    "counselor to address you?"
)

ASK_CALLBACK_TIME = (
    "Thanks, {name}! When would be a good time to call you?\n\n"
    "Our hours are {hours}.\n"
    "You can reply like: \"tomorrow 4pm\" or \"Monday 11:30 am\"."
)

ASK_REMARKS = (
    "Noted for {when}. ✅\n\n"
    "Anything specific you'd like the {team} to prepare for? "
    "Reply with a short note, or send \"skip\"."
)

LEAD_CREATED_PRE_SALES = (
    "All set, {name}! 🎉\n\n"
    "One of our counselors will call you on {phone} at {when}.\n\n"
    "If anything changes, just message me here. Have a great day!"
)

LEAD_CREATED_POST_SALES = (
    "Done, {name}! ✅\n\n"
    "Our support team will call you on {phone} at {when} to help with this.\n\n"
    "Message me anytime if you need something else."
)

CALLBACK_DECLINED = (
    "No problem at all. I'm here if you have more questions - "
    "just ask, or say \"menu\" to see the options again."
)

POST_SALES_INTRO = (
    "Good to have you back! Since you're already enrolled, let me help you directly."
)

FALLBACK_UNRECOGNISED = (
    "Sorry, I didn't quite get that. Pick an option below, or type your question "
    "and I'll do my best."
)

SESSION_RESTART = "Let's start fresh."

UNSUPPORTED_MESSAGE = (
    "I can only read text messages at the moment. Could you type your question instead? "
    "If you'd prefer to talk to a person, just say \"counselor\"."
)

ERROR_MESSAGE = (
    "Something went wrong on my side. Please try again in a moment - "
    "or say \"counselor\" and I'll have someone call you."
)


# --------------------------------------------------------------------------- #
# Callback-time rejection messages
# --------------------------------------------------------------------------- #
def time_rejection(reason: str, *, hours: str, suggestions: list[str]) -> str:
    """Explain why a requested time will not work, and offer alternatives."""
    lead = {
        "NOT_UNDERSTOOD": (
            "Sorry, I couldn't read that as a time."
        ),
        "MISSING_TIME": (
            "I got the day, but not the time - what hour suits you?"
        ),
        "IN_PAST": (
            "That time has already passed."
        ),
        "TOO_SOON": (
            "That's a little too soon for us to arrange - could you pick a slightly later slot?"
        ),
        "TOO_FAR": (
            "That's quite far ahead. Could you pick something within the next few weeks?"
        ),
        "CLOSED_DAY": (
            "We're closed that day, I'm afraid."
        ),
        "OUTSIDE_HOURS": (
            "That falls outside our calling hours."
        ),
    }.get(reason, "Sorry, that time doesn't work.")

    parts = [lead, f"We call between {hours}."]
    if suggestions:
        options = " • ".join(suggestions)
        parts.append(f"For example: {options}")
    parts.append("What time works for you?")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Rescheduling
# --------------------------------------------------------------------------- #
RESCHEDULE_PROMPT = (
    "You have a call booked for {when}.\n\n"
    "What time would suit you better?\n\n"
    "Our hours are {hours}."
)

RESCHEDULE_DONE = (
    "Done - your call has been moved to {when}. ✅\n\n"
    "The earlier slot has been released."
)

RESCHEDULE_NOTHING_BOOKED = (
    "I can't find an upcoming call booked for this number. "
    "Shall I arrange one for you?"
)


def reschedule_choice(when: str) -> OutboundMessage:
    """Asked when someone books again while a call is already on the books.

    Almost always an accident - the user forgot, or thought the first attempt
    failed. Silently adding a second booking sends a counselor to ring twice;
    silently replacing it loses a call someone genuinely wanted.
    """
    return OutboundMessage(
        text=(
            f"You already have a call booked for {when}.\n\n"
            "Would you like to move that one, or book an additional call?"
        ),
        buttons=(
            Button(id=RESCHEDULE_MOVE, title="Move it"),
            Button(id=RESCHEDULE_NEW, title="Book another"),
        ),
    )
