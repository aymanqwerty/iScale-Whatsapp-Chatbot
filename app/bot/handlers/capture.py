"""Slot filling for a callback booking.

The old chain asked one fixed question per turn: name, then time, then remarks.
That falls apart the moment someone volunteers more than one thing at once -
"I'm Meera, meera@x.com, Data Science batch, call me tomorrow at 4" used to
answer exactly one question and then ask for the rest, which reads as though
nobody was listening.

So capture is a *set of slots*, not a sequence of states. Every turn absorbs
whatever the message happens to contain, then asks only for what is still
missing. The state machine still owns the rule that matters - a lead is not
written until the required slots are full - which is what keeps a post-sales
booking impossible without an email.

Extraction here is deliberately deterministic rather than a second LLM call.
Emails, phone numbers and times have unambiguous shapes; a model round-trip
would add latency to every capture turn and introduce a way for the booking to
be wrong that no test could reliably catch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.bot import copy, intents
from app.bot.context import (
    CTX_CONTACT_PHONE,
    CTX_EMAIL,
    CTX_ENROLLED_COURSE,
    CTX_PENDING_NAME,
    CTX_PENDING_TIME,
    CTX_RESCHEDULE_LEAD_ID,
    TurnContext,
)
from app.bot.handlers.common import clean_name
from app.core.logging import get_logger
from app.domain.enums import ConversationState, LeadType
from app.domain.messaging import OutboundMessage, TurnResult

logger = get_logger(__name__)

#: Permissive on purpose. This decides whether we *found* an address, not
#: whether it can receive mail - rejecting a valid-but-unusual address costs a
#: booking, while a typo is something the support team sees and fixes.
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@.]+\.[^\s@]{2,}")

#: An Indian mobile number, with or without country code, spaces or dashes.
#: Anchored on a 10-digit run starting 6-9 so it cannot match a year, a price
#: or the "4" in "call me at 4pm".
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?91[\s\-]?)?([6-9]\d{9})(?!\d)"
)


@dataclass(frozen=True, slots=True)
class MissingSlot:
    """A slot still to be filled, and the question that fills it."""

    key: str
    label: str


def required_slots(lead_type: LeadType | None) -> tuple[str, ...]:
    """Slots that must be full before a lead may be written.

    Post-sales carries three extra requirements because a support call is
    against an *account*: without an email and a course there is nothing for the
    team to look up, and they would spend the call establishing who they are
    talking to. Pre-sales has no such need - a counselor calling a new lead only
    needs a name, a number and a time.
    """
    if lead_type is LeadType.POST_SALES:
        return (CTX_PENDING_NAME, CTX_EMAIL, CTX_ENROLLED_COURSE, CTX_PENDING_TIME)
    return (CTX_PENDING_NAME, CTX_PENDING_TIME)


#: Human names for the slots, used when explaining what is still needed.
_LABELS: dict[str, str] = {
    CTX_PENDING_NAME: "name",
    CTX_EMAIL: "email address",
    CTX_ENROLLED_COURSE: "enrolled course",
    CTX_PENDING_TIME: "preferred time",
}


def absorb_slots(ctx: TurnContext) -> set[str]:
    """Fill every slot this message happens to contain. Returns what was filled.

    Order matters. Email and phone are pulled out first and removed from the
    text before a name is looked for, otherwise "meera@x.com" is a perfectly
    plausible-looking name to the name cleaner.
    """
    text = (ctx.text or "").strip()
    if not text:
        return set()

    conversation = ctx.conversation
    filled: set[str] = set()
    remaining = text

    email = _EMAIL_RE.search(remaining)
    if email and not conversation.get_ctx(CTX_EMAIL):
        # Trailing punctuation is stripped rather than excluded by the pattern:
        # tightening the TLD to letters would break "name@firm.co.in", which is
        # a very common shape here.
        address = email.group(0).rstrip(".,;:!?)")
        conversation.set_ctx(CTX_EMAIL, address)
        filled.add(CTX_EMAIL)
        remaining = remaining.replace(email.group(0), " ")

    phone = _PHONE_RE.search(remaining)
    if phone and not conversation.get_ctx(CTX_CONTACT_PHONE):
        # Stored normalised to the same shape as the WhatsApp sender id, so the
        # sheet's two phone columns are comparable at a glance.
        conversation.set_ctx(CTX_CONTACT_PHONE, f"91{phone.group(1)}")
        filled.add(CTX_CONTACT_PHONE)
        remaining = remaining.replace(phone.group(0), " ")

    if not conversation.get_ctx(CTX_ENROLLED_COURSE):
        course = ctx.deps.knowledge_base.match_course(remaining)
        if course is not None:
            conversation.set_ctx(CTX_ENROLLED_COURSE, course.name)
            filled.add(CTX_ENROLLED_COURSE)
            # Take the course title out of the running text too, or it is still
            # sitting there when the name is looked for.
            remaining = re.sub(
                re.escape(course.name), " ", remaining, flags=re.IGNORECASE
            )

    if not conversation.get_ctx(CTX_PENDING_NAME):
        candidate = _find_name(remaining)
        if candidate:
            conversation.set_ctx(CTX_PENDING_NAME, candidate)
            filled.add(CTX_PENDING_NAME)

    if filled:
        logger.info("Absorbed capture slots", extra={"slots": sorted(filled)})
    return filled


def _find_name(text: str) -> str | None:
    """Pull a name out of a message that may contain several other things.

    `clean_name` validates a whole string, which is right when the message is
    only an answer to "what is your name?" but useless for "I am Meera,
    9812345678, Data Science" - one stray comma and the entire thing is
    rejected. So the whole string is tried first, then each comma- or
    newline-separated fragment, taking the first that reads like a name.
    """
    whole = clean_name(text)
    if whole:
        return whole
    for fragment in re.split(r"[,\n;]+", text):
        fragment = fragment.strip()
        if not fragment:
            continue
        candidate = clean_name(fragment)
        if candidate:
            return candidate
    return None


def missing_slots(ctx: TurnContext) -> list[MissingSlot]:
    """Required slots that are still empty, in the order they should be asked."""
    conversation = ctx.conversation

    # A returning contact carries their name on the user record, not in this
    # conversation's context - and a lead created earlier closes its
    # conversation, so a reschedule always starts with an empty one. Without
    # this, rescheduling asked a known customer for their name again.
    if not conversation.get_ctx(CTX_PENDING_NAME) and ctx.user.name:
        conversation.set_ctx(CTX_PENDING_NAME, ctx.user.name)

    return [
        MissingSlot(key=key, label=_LABELS.get(key, key))
        for key in required_slots(conversation.lead_type)
        if not conversation.get_ctx(key)
    ]


def missing_labels(ctx: TurnContext) -> str:
    """"email address and enrolled course" - for the refusal message."""
    labels = [slot.label for slot in missing_slots(ctx) if slot.key != CTX_PENDING_TIME]
    if not labels:
        return "details"
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def next_question(ctx: TurnContext) -> TurnResult | None:
    """Ask for the next missing slot, or None when capture is complete.

    Phone is asked last among the non-time slots and only once, because it is
    the one question the user has effectively already answered by messaging us -
    it is a confirmation, not an interrogation.
    """
    conversation = ctx.conversation
    result = TurnResult()

    # A reschedule is moving a booking that already exists, so everything it
    # needs was captured the first time. Asking again would turn "move my call"
    # into a second full interrogation.
    if conversation.get_ctx(CTX_RESCHEDULE_LEAD_ID):
        return None

    for slot in missing_slots(ctx):
        if slot.key == CTX_PENDING_TIME:
            continue  # asked below, after the identifying details
        if slot.key == CTX_PENDING_NAME:
            conversation.current_state = ConversationState.ASK_NAME
            result.add(OutboundMessage(text=copy.ASK_NAME))
            return result
        if slot.key == CTX_EMAIL:
            conversation.current_state = ConversationState.ASK_EMAIL
            result.add(OutboundMessage(text=copy.ASK_EMAIL))
            return result
        if slot.key == CTX_ENROLLED_COURSE:
            conversation.current_state = ConversationState.ASK_ENROLLED_COURSE
            result.add(OutboundMessage(text=copy.ASK_ENROLLED_COURSE))
            return result

    if not conversation.get_ctx(CTX_CONTACT_PHONE) and not conversation.get_ctx(
        _ASKED_PHONE
    ):
        conversation.set_ctx(_ASKED_PHONE, True)
        conversation.current_state = ConversationState.ASK_PHONE
        result.add(copy.phone_confirm(_pretty(ctx.user.phone)))
        return result

    if not conversation.get_ctx(CTX_PENDING_TIME):
        from app.bot.handlers.common import ask_time_text

        name = str(conversation.get_ctx(CTX_PENDING_NAME) or "")
        conversation.current_state = ConversationState.ASK_CALLBACK_TIME
        result.add(OutboundMessage(text=ask_time_text(ctx, name)))
        return result

    return None


#: Set once the phone confirmation has been shown, so a user who ignores it and
#: answers something else is not asked the same thing on every later turn.
_ASKED_PHONE = "asked_phone"


def confirm_phone(ctx: TurnContext) -> bool:
    """Handle a reply to the phone confirmation. True if it was consumed."""
    conversation = ctx.conversation
    reply_id = ctx.inbound.reply_id

    if reply_id == copy.PHONE_CONFIRM or (
        reply_id is None and intents.is_affirmative(ctx.text)
    ):
        conversation.set_ctx(CTX_CONTACT_PHONE, ctx.user.phone)
        return True

    if reply_id == copy.PHONE_OTHER:
        # They want a different number but have not typed one yet; leaving the
        # slot empty means the next turn's absorb picks it up.
        return True

    return bool(absorb_slots(ctx) & {CTX_CONTACT_PHONE})


def _pretty(phone: str) -> str:
    """"919876543210" -> "+91 98765 43210"."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        return f"+91 {digits[2:7]} {digits[7:]}"
    return phone
