"""All user-facing text and menu definitions.

Kept in one module so the tone of the bot can be reviewed and edited by a
non-developer without touching flow logic, and so nothing is hard-coded inside a
handler.
"""

from __future__ import annotations

from app.domain.messaging import MAX_BUTTONS, Button, ListRow, OutboundMessage
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

#: Course-group rows, mirroring the site's own grouping. Only these two groups
#: are ever offered; `foundation` and `free` courses stay answerable by name but
#: the funnel never volunteers them.
GROUP_PREFIX = "group:"
GROUP_COHORT = "group:cohort"
GROUP_ADVANCE = "group:advance"

#: "Already Enrolled" splits here before anything else is asked. A free-course
#: student gets no callback - they re-enter the pre-sales funnel instead.
ENROLLED_FREE = "enrolled:free"
ENROLLED_PAID = "enrolled:paid"

SUPPORT_PREFIX = "support:"

RESCHEDULE_MOVE = "reschedule:move"
RESCHEDULE_NEW = "reschedule:new"

CONFIRM_YES = "confirm:yes"
CONFIRM_NO = "confirm:no"

#: Phone confirmation during capture.
PHONE_CONFIRM = "phone:confirm"
PHONE_OTHER = "phone:other"

#: Replies to the discount offer. "Talk to a Counselor" reuses `MENU_COUNSELOR`
#: so the escalation path is identical wherever it is tapped from.
OFFER_DONE = "offer:done"
OFFER_QUESTION = "offer:question"
#: "Send me the payment link" - the direct-payment route that undercuts the
#: coupon. Takes the primary button slot whenever direct payment is enabled.
OFFER_PAY_NOW = "offer:pay"

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

#: Three options, not six. A paid student is about to be asked for name, email
#: and enrolled course, so the issue menu has to be quick - it is a routing
#: label for the support team, not a diagnosis.
#:
#: The retired ids (`support:assignment`, `:placement`, `:certificate`,
#: `:timing`) are still matched by `issue_label_for` below. WhatsApp messages
#: never expire, so someone scrolling back to an old menu can still tap one, and
#: a dropped branch would answer them with "I didn't understand that".
SUPPORT_OPTIONS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        f"{SUPPORT_PREFIX}video",
        "Video Related",
        "Playback, access or content issues",
        ("video", "lecture", "playback", "buffering", "recording", "not playing"),
    ),
    (
        f"{SUPPORT_PREFIX}technical",
        "Technical Issue",
        "Login, portal or access problems",
        ("technical", "login", "portal", "password", "access", "error", "bug", "app"),
    ),
    (
        f"{SUPPORT_PREFIX}other",
        "Other",
        "Something else",
        ("other", "else", "different", "assignment", "placement", "certificate", "timing"),
    ),
)

#: Ids that were on the old six-row support menu. Mapped rather than dropped.
_RETIRED_SUPPORT_IDS: dict[str, str] = {
    f"{SUPPORT_PREFIX}assignment": "Assignment",
    f"{SUPPORT_PREFIX}placement": "Placement",
    f"{SUPPORT_PREFIX}certificate": "Certificate",
    f"{SUPPORT_PREFIX}timing": "Class Timing",
}


def issue_label_for(option_id: str) -> str | None:
    """Human label for a support option id, including retired ones."""
    for oid, title, _, _ in SUPPORT_OPTIONS:
        if oid == option_id:
            return title
    return _RETIRED_SUPPORT_IDS.get(option_id)


# --------------------------------------------------------------------------- #
# Menu builders
# --------------------------------------------------------------------------- #
def _main_menu_buttons() -> tuple[Button, ...]:
    """The three main-menu options as inline quick-reply buttons.

    Buttons rather than a list because there are only three of them. A list
    collapses behind a "Choose an option" tap, so the user has to open it before
    they can see what is on offer; buttons sit directly under the text and are
    one tap. WhatsApp allows at most three, which is exactly what this menu has
    - `OutboundMessage` raises if that is ever exceeded, and the longest title
    ("Talk to a Counselor", 19 chars) is inside the 20-character button limit.
    Anything longer would be silently truncated, so `test_button_menu_titles_fit`
    guards the width.
    """
    return tuple(
        Button(id=oid, title=title) for oid, title, _, _ in MAIN_MENU_OPTIONS
    )


def welcome_message(company: str, *, returning_name: str | None = None) -> OutboundMessage:
    # No header: WhatsApp renders it as a bold line above the text, and "Main
    # menu" there told the user nothing they could not see from the buttons.
    greeting = f"Hey {returning_name}! 👋" if returning_name else "Hey! 👋"
    return OutboundMessage(
        text=f"{greeting} I'm {company}'s AI Agent 🤖\n\nHow can I help you today?",
        buttons=_main_menu_buttons(),
    )


def main_menu(prompt: str = "What would you like to do next?") -> OutboundMessage:
    return OutboundMessage(text=prompt, buttons=_main_menu_buttons())


def _choices(
    text: str,
    options: list[tuple[str, str]],
    *,
    list_button_label: str = "Choose",
    header: str | None = None,
) -> OutboundMessage:
    """Render (id, title) pairs as buttons, or a list once there are too many.

    Buttons are the better widget: they sit under the text and are one tap,
    where a list hides everything behind "Choose an option". But WhatsApp allows
    only three, and these menus are built from `courses.json` - so adding a
    fourth advance course would otherwise raise at send time, in production, on
    a live conversation. Falling back to a list keeps that a formatting change
    instead of an outage.
    """
    if len(options) <= MAX_BUTTONS:
        return OutboundMessage(
            text=text,
            buttons=tuple(Button(id=oid, title=title) for oid, title in options),
        )
    return OutboundMessage(
        text=text,
        list_rows=tuple(ListRow(id=oid, title=title) for oid, title in options),
        list_button_label=list_button_label,
        header=header,
    )


def course_group_menu() -> OutboundMessage:
    """First level of the pre-sales branch: which kind of course.

    Cohort is offered first deliberately - it holds AI For Everyone, which is
    the course this funnel is built to sell.
    """
    return _choices(
        "Great! What are you looking for?",
        [
            (GROUP_COHORT, "Cohort Courses"),
            (GROUP_ADVANCE, "Advance Courses"),
            (COURSE_UNSURE, "Not sure yet"),
        ],
    )


def course_group_submenu(
    knowledge_base: KnowledgeBase, group: str
) -> OutboundMessage:
    """The courses inside one group, in the order `courses.json` specifies."""
    courses = knowledge_base.courses_in_group(group)
    prompt = (
        "Both of these are beginner friendly - no coding background needed.\n\n"
        "Which one shall I tell you about?"
        if group == "cohort"
        else "Our advance programs. Which one would you like to know about?"
    )
    # No "Not sure yet" row here. The spec is two rows for cohort and three for
    # advance, and a fourth would tip advance over WhatsApp's three-button limit
    # into a list - collapsing the courses behind an extra tap. Anyone still
    # undecided can say so, which `means_undecided` catches.
    options = [
        (f"{COURSE_PREFIX}{course.slug}", course.menu_label) for course in courses
    ]
    return _choices(prompt, options, list_button_label="View courses", header="Courses")


def enrollment_type_menu() -> OutboundMessage:
    """Splits "Already Enrolled" before any details are asked.

    Free-course students are not offered a callback at all, so the split has to
    happen before the support menu rather than after it.
    """
    return _choices(
        "Got it! Which one are you enrolled in?",
        [
            (ENROLLED_PAID, "Paid Course"),
            (ENROLLED_FREE, "Free Course"),
        ],
    )


def course_menu() -> OutboundMessage:
    """Entry point to the course branch.

    Kept as the name every caller already uses; it now opens the group menu
    rather than listing every featured course at once, so it no longer needs the
    knowledge base.
    """
    return course_group_menu()


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
    return _choices(
        "Sure - what do you need help with?",
        [(oid, title) for oid, title, _, _ in SUPPORT_OPTIONS],
        list_button_label="Choose a topic",
        header="Student support",
    )


def offer_message(offer: dict[str, object], course_name: str) -> OutboundMessage:
    """The chatbot-exclusive discount, rendered from `offers.json`.

    Assembled here from exact values rather than written by the model, and the
    offer is deliberately absent from the retrieval index. A model asked to
    "mention the discount" will eventually produce the wrong percentage or a
    mistyped coupon - and every one of those is a price the business must then
    either honour or publicly withdraw.

    Both routes are always offered. A discount is not a reason to take the
    counselor away from someone who wants one: plenty of people will not put a
    card into a link a chatbot sent them, and losing those is worse than the
    margin saved.
    """
    code = str(offer.get("coupon_code", ""))
    percent = offer.get("discount_percent")
    saving = offer.get("discount_inr")
    was = offer.get("list_price_inr")
    now = offer.get("final_price_inr")
    url = str(offer.get("payment_url", ""))
    up_to = bool(offer.get("discount_is_maximum"))

    if up_to:
        # "Up to" means the coupon may give less, so the price is quoted as a
        # best case rather than a struck-through certainty. Showing
        # "~4,999~ -> 3,749" here would promise a number checkout might not
        # honour, and being contradicted at the payment page loses the sale and
        # the trust together.
        headline = f"🎁 *Up to {percent}% off {course_name}*"
        pricing = f"That's as low as *₹{now:,}* instead of ₹{was:,}"
    else:
        headline = f"🎁 *{percent}% off {course_name}*"
        pricing = f"~₹{was:,}~  →  *₹{now:,}*  (you save ₹{saving:,})"

    direct = offer.get("direct_payment")
    direct_on = isinstance(direct, dict) and bool(direct.get("enabled"))

    text = (
        f"Since we've been chatting, I can give you something that isn't on the "
        f"website 👇\n\n"
        f"{headline}\n"
        f"Use code *{code}* at checkout\n"
        f"{pricing}\n\n"
        f"{url}\n\n"
        f"⏳ This code is only for people who reach us here on WhatsApp, and "
        f"it won't stay open long.\n\n"
    )

    if direct_on:
        # The better-than-coupon route, stated as a comparison rather than a
        # second option in a list - "there is something better than the thing I
        # just gave you" is the part that makes someone tap rather than leave to
        # think about it. No figure: Razorpay owns the amount, and a number
        # written here would eventually contradict the checkout page.
        # No second mention of the coupon code. Naming it twice made the two
        # routes read as competing offers the customer had to compare, when the
        # only thing they need to understand is "this one is cheaper".
        text += (
            "✨ *Want it even cheaper?*\n"
            "Pay right here through this chat and I'll give you our biggest "
            "discount - even lower than the price above. It's the best price "
            "we offer anywhere.\n\n"
            "Shall I send you the payment link?"
        )
        primary = Button(id=OFFER_PAY_NOW, title="Yes, best price")
    else:
        text += "Want to go ahead, or shall I have a counselor talk you through it first?"
        primary = Button(id=OFFER_DONE, title="I'll enroll now")

    # Three is WhatsApp's hard limit, so the direct-payment route takes the
    # primary slot when it is on. Counselor stays regardless: plenty of people
    # will not put a card into a link a chatbot sent them, and losing those is
    # worse than the margin saved.
    return OutboundMessage(
        text=text,
        buttons=(
            primary,
            Button(id=MENU_COUNSELOR, title="Talk to a Counselor"),
            Button(id=OFFER_QUESTION, title="I have a question"),
        ),
    )


def payment_links(offer: dict[str, object], course_name: str) -> OutboundMessage:
    """Both Razorpay links, plus what to do after paying.

    Both are shown together and clearly labelled rather than picked for the
    customer. Guessing from the country code would be right most of the time,
    and the times it is wrong are a failed payment at the exact moment someone
    decided to buy - which is the single most expensive place in this funnel to
    be clever.

    The screenshot request is the whole reason this reads as a complete
    transaction: nothing here can see a Razorpay payment, so the customer has to
    hand us the proof, and being told that up front is what stops them paying
    and then wondering if anything happened.
    """
    direct = offer.get("direct_payment")
    direct = direct if isinstance(direct, dict) else {}
    india = str(direct.get("india_url", ""))
    world = str(direct.get("international_url", ""))
    code = str(offer.get("coupon_code", ""))
    site = str(offer.get("payment_url", ""))

    if not india and not world:
        # Configuration slipped. Falling back to the coupon keeps the sale alive;
        # sending a message with an empty link in it would not.
        return OutboundMessage(text=OFFER_ACCEPTED.format(code=code, url=site))

    return OutboundMessage(
        text=(
            f"Perfect! 🙌 Here's your link for *{course_name}* - this price is "
            f"lower than {code} and it's the best we offer anywhere.\n\n"
            f"🇮🇳 *Paying from India:*\n{india}\n\n"
            f"🌍 *Paying from outside India:*\n{world}\n\n"
            f"Just pick the one that matches where you are.\n\n"
            f"📸 *One last thing* - once you've paid, send me a *screenshot of "
            f"the payment right here* and our team will confirm your seat, "
            f"usually within a few hours.\n\n"
            f"Any trouble at all, say \"counselor\" and someone will call you."
        ),
        buttons=(Button(id=MENU_COUNSELOR, title="I need help"),),
    )


def phone_confirm(phone: str) -> OutboundMessage:
    """Confirm the WhatsApp number, or invite a different one."""
    return OutboundMessage(
        text=ASK_PHONE.format(phone=phone),
        buttons=(
            Button(id=PHONE_CONFIRM, title="Yes, this one"),
            Button(id=PHONE_OTHER, title="Different number"),
        ),
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

#: Opens the discovery branch. Asks one thing, not three - a single easy
#: question gets answered, a form does not. What they do is the hook the whole
#: AI For Everyone pitch hangs on, so it is the one thing worth asking for.
UNSURE_COURSE = (
    "No problem at all - most people start here! 😊\n\n"
    "Just tell me what you do right now - studying, working, running your own "
    "thing - and I'll show you exactly where AI fits into it."
)

#: A free-course student asking for support. We do not staff callbacks for free
#: courses, so this says so plainly and then does the one useful thing left:
#: treats them as a warm pre-sales lead, because they have already shown intent.
FREE_COURSE_NO_SUPPORT = (
    "Thanks for learning with us! 🙌\n\n"
    "Quick heads-up: our free courses don't come with one-on-one support or "
    "counselor calls - they're self-paced, so everything runs through the app.\n\n"
    "That said, since you've already made a start - what are you doing at the "
    "moment, studying or working? I'll show you what to pick up next."
)

GENERAL_QNA_INTRO = (
    "Of course - go ahead and ask. I can help with course comparisons, "
    "learning roadmaps, prerequisites and career questions."
)

#: Phone confirmation. We already know the WhatsApp number, so asking them to
#: type ten digits they have already effectively given us is pure friction -
#: but a counselor ringing the WhatsApp number when they wanted their office
#: line loses the call, so it is confirmed rather than assumed.
ASK_PHONE = (
    "And is {phone} the best number to call you on?\n\n"
    "Tap to confirm, or just send me a different number."
)

ASK_EMAIL = (
    "What's your email address? We use it to pull up your enrollment before "
    "the team calls."
)

ASK_ENROLLED_COURSE = "Which course are you enrolled in?"

#: Shown when a paid student will not give the details a support call needs.
#: Deliberately not a dead end: it explains the reason, leaves the conversation
#: open, and never scolds. They may simply have mistyped an email.
POST_SALES_DETAILS_REQUIRED = (
    "I'm sorry - I can't book a support call without your {missing}. 🙏\n\n"
    "It's how the team pulls up your enrollment before they ring, so they're "
    "not asking you to prove who you are on the call itself.\n\n"
    "Happy to take it whenever you're ready, and I can still answer anything "
    "else in the meantime."
)

#: After "I'll enroll now". Deliberately does not congratulate them on a
#: purchase - we cannot see the payment, and treating a tap as a completed sale
#: would be a lie the moment they close the page.
OFFER_ACCEPTED = (
    "Brilliant! 🎉\n\n"
    "Just apply *{code}* at checkout here:\n{url}\n\n"
    "If the code gives you any trouble, or you'd rather someone walked you "
    "through it, say \"counselor\" and I'll arrange a call."
)

#: Sent when someone posts an image after being asked for payment proof.
#:
#: Carefully does NOT confirm the payment. Nothing here can read a Razorpay
#: transaction, and an image could be anything - a wrong screenshot, a failed
#: attempt, a photo of a cat. "Received, a human will check it" is true;
#: "your seat is confirmed" would be a promise made by something that cannot
#: see the money.
PAYMENT_PROOF_ACK = (
    "Got it, thank you! 🎉\n\n"
    "I've passed your payment screenshot to our team - they'll verify it and "
    "confirm your seat, usually within a few hours. You'll hear from us right "
    "here.\n\n"
    "Anything you'd like to know in the meantime, just ask 😊"
)

#: Someone sent media without ever being sent a payment link. Not treated as
#: proof - it is far more likely a screenshot of a question - but it must not
#: get the flat "I only read text" brush-off either, in case it IS a payment
#: they arranged another way.
MEDIA_WITHOUT_PAYMENT = (
    "Thanks for sending that! I can't open images myself, so could you tell me "
    "in a message what it's about?\n\n"
    "If it's a payment you've already made, say \"payment\" and I'll get "
    "someone from our team to check it for you."
)

OFFER_QUESTION_PROMPT = (
    "Of course - ask away! 😊 Happy to go through anything: what's covered, "
    "how the classes work, or what you'd actually be able to build."
)

#: Two nudges when a conversation goes quiet: one after an hour, one six hours
#: after that. Neither ends anything - the conversation stays exactly where it
#: was, so replying "yes" an hour later continues the same thread rather than
#: starting a new one.
#:
#: Nothing here says "closing", "resolving" or "ending". The whole point is to
#: get someone back, and telling them the door has shut does the opposite.
#:
#: The booking versions name what was left unfinished. Someone who stopped
#: halfway through giving their details is the most recoverable lead in the
#: funnel, and a generic "are you still there?" wastes that.
INACTIVITY_BOOKING = (
    "{greeting}! 👋 Looks like life got busy — totally fair.\n\n"
    "We were *this* close to getting your call booked. Want to finish it off? "
    "It'll take about ten seconds. 😄"
)

INACTIVITY_GENERAL = (
    "{greeting}! 👋 You went quiet on me — busy day?\n\n"
    "No rush at all. Whenever you're free, just pick up right where we left "
    "off and I'll be here. 😊"
)

INACTIVITY_BOOKING_LAST = (
    "{greeting}! 😄 Me again — last nudge, promise.\n\n"
    "Your call is still half-booked and I'd hate for it to go to waste. "
    "Say the word and we'll finish it in seconds — or ignore me and I'll stop "
    "bothering you. 🙈"
)

INACTIVITY_GENERAL_LAST = (
    "{greeting}! 😄 Just checking in one last time.\n\n"
    "Still happy to help with courses, fees or a call with a counselor — "
    "whenever suits you. I'll leave you be now, but I'm one message away. 👋"
)


#: Replies to a sign-off. One short line, no menu, no model call - the point is
#: to let the conversation end gracefully instead of restarting it. Varied only
#: by whether we know their name; anything more elaborate would itself be noise.
def farewell(name: str | None = None) -> str:
    who = f", {name.split()[0]}" if name else ""
    return (
        f"Anytime{who}! 😊 I'm right here if anything else comes up — "
        "just message me."
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
