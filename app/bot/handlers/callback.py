"""Callback-capture handlers: consent, name, time, remarks, lead creation."""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.bot import copy, intents
from app.bot.handlers.capture import (
    absorb_slots,
    confirm_phone,
    missing_labels,
    next_question,
)
from app.bot.context import (
    CTX_NAME_ATTEMPTS,
    CTX_PENDING_DATE,
    CTX_CONTACT_PHONE,
    CTX_EMAIL,
    CTX_ENROLLED_COURSE,
    CTX_PENDING_NAME,
    CTX_PROFILE,
    CTX_PENDING_TIME,
    CTX_PENDING_TIME_RAW,
    CTX_RESCHEDULE_LEAD_ID,
    CTX_RETURN_STATE,
    CTX_SUPPORT_TOPIC,
    TurnContext,
)
from app.bot.handlers.common import (
    answer_question,
    ask_time_text,
    clean_name,
    start_callback_capture,
)
from app.core.logging import get_logger
from app.db.models.lead import Lead
from app.domain.enums import ConversationState, LeadType
from app.domain.messaging import OutboundMessage, TurnResult
from app.services.scheduling.callback_time import CallbackSlot

logger = get_logger(__name__)

#: After this many unusable replies we stop asking and accept whatever was sent,
#: rather than trapping the user in a loop over a name field.
_MAX_NAME_ATTEMPTS = 2

#: Times a required post-sales slot is asked for before we stop and explain.
#: One retry covers a typo; a third ask reads as badgering.
_MAX_SLOT_ATTEMPTS = 2


async def handle_ask_callback(ctx: TurnContext) -> TurnResult:
    """Yes / no to "shall a counselor call you?"."""
    result = TurnResult()
    conversation = ctx.conversation
    reply_id = ctx.inbound.reply_id

    accepted = reply_id == copy.CONFIRM_YES or (
        reply_id is None and (intents.is_affirmative(ctx.text) or intents.wants_human(ctx.text))
    )
    declined = reply_id == copy.CONFIRM_NO or (
        reply_id is None and intents.is_negative(ctx.text)
    )

    if accepted:
        lead_type = conversation.lead_type or LeadType.PRE_SALES
        return start_callback_capture(ctx, lead_type=lead_type)

    resume = _resume_state(ctx)

    if declined:
        conversation.current_state = resume
        result.add(OutboundMessage(text=copy.CALLBACK_DECLINED))
        return result

    # Not an answer to the question - the user carried on asking things.
    # Answer it and stay available rather than insisting on a yes or no.
    conversation.current_state = resume
    answer = await answer_question(ctx)
    ctx.bump_qna_count()
    result.add(OutboundMessage(text=answer))
    return result


async def handle_ask_email(ctx: TurnContext) -> TurnResult:
    """Post-sales: take the email, or explain why we cannot book without it."""
    return await _capture_turn(ctx, expecting=CTX_EMAIL)


async def handle_ask_enrolled_course(ctx: TurnContext) -> TurnResult:
    """Post-sales: which course they are enrolled in."""
    return await _capture_turn(ctx, expecting=CTX_ENROLLED_COURSE)


async def handle_ask_phone(ctx: TurnContext) -> TurnResult:
    """Confirm the WhatsApp number, or take a different one.

    Never a dead end for the message itself. Someone answering "tomorrow 11:30"
    here has moved on to the next question in their head, and swallowing that as
    a failed phone answer loses the time entirely - the user then has to say it
    twice. So an answer that is plainly not a number is taken as tacit
    acceptance of the WhatsApp number, and the message is passed to whatever we
    would have asked next.
    """
    if confirm_phone(ctx):
        follow_up = next_question(ctx)
        if follow_up is not None:
            return follow_up
        return await _create_lead(ctx, remarks=None)

    # Not a number and not a confirmation: accept the WhatsApp number and let
    # the message answer the next question instead of discarding it.
    ctx.conversation.set_ctx(CTX_CONTACT_PHONE, ctx.user.phone)
    if not ctx.conversation.get_ctx(CTX_PENDING_TIME):
        ctx.conversation.current_state = ConversationState.ASK_CALLBACK_TIME
        return await handle_ask_callback_time(ctx)

    follow_up = next_question(ctx)
    if follow_up is not None:
        return follow_up
    return await _create_lead(ctx, remarks=None)


async def _capture_turn(ctx: TurnContext, *, expecting: str) -> TurnResult:
    """One slot-filling turn: absorb whatever arrived, then ask for the rest.

    If the user declines the specific thing we asked for, say plainly why it is
    needed and stop - without ending the conversation. They may have mistyped an
    address, or want to check it; a booking refused politely can still happen
    five messages later, and a dead end cannot.
    """
    filled = absorb_slots(ctx)

    # A tapped button is never a refusal of this question - it is a stray tap on
    # an older message, and `is_skip("")` is true for the empty text a tap
    # carries. Without this guard, tapping anything at all during the email
    # question was read as "I refuse to give it".
    conversation = ctx.conversation
    declined = (
        not ctx.inbound.reply_id
        and bool(ctx.text.strip())
        and (
            intents.is_skip(ctx.text)
            or intents.declines_to_share(ctx.text)
        )
    )

    if expecting not in filled and not conversation.get_ctx(expecting):
        # Bounded by attempts as well as by an explicit refusal. People decline
        # in sentences a keyword list will never cover ("I'd rather not share
        # that"), and asking the same question forever is worse than explaining
        # once why we cannot proceed.
        key = f"attempts:{expecting}"
        attempts = int(conversation.get_ctx(key, 0)) + 1
        conversation.set_ctx(key, attempts)

        if declined or attempts > _MAX_SLOT_ATTEMPTS:
            result = TurnResult()
            result.add(
                OutboundMessage(
                    text=copy.POST_SALES_DETAILS_REQUIRED.format(
                        missing=missing_labels(ctx)
                    )
                )
            )
            # Back to answering questions; nothing has been written, and the
            # conversation stays open so they can still book later.
            conversation.current_state = ConversationState.SUPPORT_QUERY
            return result
    else:
        conversation.clear_ctx(f"attempts:{expecting}")

    follow_up = next_question(ctx)
    if follow_up is not None:
        return follow_up
    return await _create_lead(ctx, remarks=None)


async def handle_ask_name(ctx: TurnContext) -> TurnResult:
    """Capture the user's name."""
    result = TurnResult()
    conversation = ctx.conversation

    # Absorb first. "I am Meera, meera@x.com, 9812345678, Master Of Data
    # Analytics" is one message answering four questions, and `clean_name` on
    # the raw text would try to read the whole thing as a name. Absorption
    # strips the email and number out before the name is looked for.
    absorb_slots(ctx)

    name = conversation.get_ctx(CTX_PENDING_NAME) or clean_name(ctx.text)
    attempts = int(conversation.get_ctx(CTX_NAME_ATTEMPTS, 0))

    if name is None:
        if attempts < _MAX_NAME_ATTEMPTS:
            conversation.set_ctx(CTX_NAME_ATTEMPTS, attempts + 1)
            result.add(OutboundMessage(text=copy.ASK_NAME_RETRY))
            return result
        # Give up gracefully: the profile name, or nothing, beats a loop.
        name = ctx.user.profile_name or "there"

    ctx.user.name = name
    conversation.set_ctx(CTX_PENDING_NAME, name)
    conversation.clear_ctx(CTX_NAME_ATTEMPTS)

    follow_up = next_question(ctx)
    if follow_up is not None:
        return follow_up
    return await _create_lead(ctx, remarks=None)


async def handle_ask_callback_time(ctx: TurnContext) -> TurnResult:
    """Parse and validate the requested callback slot."""
    result = TurnResult()
    conversation = ctx.conversation
    validator = ctx.deps.callback_validator

    # A name volunteered here ("my name is Ayush Raj, book me for 4pm") would
    # otherwise be lost: this state only looks for a time.
    _capture_volunteered_name(ctx)

    remembered = _remembered_date(ctx)
    parsed = validator.parse(ctx.text, assume_date=remembered)

    if not parsed.ok:
        # Keep the date they named so the retry does not silently move the day.
        if parsed.parsed_date is not None:
            conversation.set_ctx(CTX_PENDING_DATE, parsed.parsed_date.isoformat())
        suggestions = [slot.display() for slot in parsed.suggestions]
        result.add(
            OutboundMessage(
                text=copy.time_rejection(
                    str(parsed.reason),
                    hours=validator.business_hours_text(),
                    suggestions=suggestions,
                )
            )
        )
        return result

    conversation.clear_ctx(CTX_PENDING_DATE)

    slot: CallbackSlot = parsed.slot  # type: ignore[assignment]
    conversation.update_ctx(
        **{
            CTX_PENDING_TIME: slot.at.isoformat(),
            CTX_PENDING_TIME_RAW: ctx.text.strip()[:255],
        }
    )
    # A message like "meera@x.com, tomorrow 4pm" carries more than a time.
    absorb_slots(ctx)

    # Anything still required is asked before the optional remarks question.
    follow_up = next_question(ctx)
    if follow_up is not None:
        return follow_up

    conversation.current_state = ConversationState.ASK_REMARKS

    team = "counselor" if conversation.lead_type is not LeadType.POST_SALES else "support team"
    result.add(
        OutboundMessage(text=copy.ASK_REMARKS.format(when=slot.display(), team=team))
    )
    return result


async def handle_ask_remarks(ctx: TurnContext) -> TurnResult:
    """Take an optional note, then create the lead."""
    remarks = None if intents.is_skip(ctx.text) else ctx.text.strip()[:2000]
    return await _create_lead(ctx, remarks=remarks)


async def handle_lead_created(ctx: TurnContext) -> TurnResult:
    """Defensive: the conversation is normally closed at this point.

    Reached only if a message arrives before the close is committed; restart
    cleanly instead of stalling.
    """
    from app.bot.handlers.menu import handle_start

    ctx.conversation.current_state = ConversationState.START
    return await handle_start(ctx)


async def handle_end(ctx: TurnContext) -> TurnResult:
    from app.bot.handlers.menu import handle_start

    ctx.conversation.current_state = ConversationState.START
    return await handle_start(ctx)


# --------------------------------------------------------------------------- #
# Lead creation
# --------------------------------------------------------------------------- #
async def _create_lead(ctx: TurnContext, *, remarks: str | None) -> TurnResult:
    result = TurnResult()
    conversation = ctx.conversation
    user = ctx.user

    lead_type = conversation.lead_type or LeadType.PRE_SALES
    name = conversation.get_ctx(CTX_PENDING_NAME) or user.display_name

    preferred_time: datetime | None = None
    raw_time = conversation.get_ctx(CTX_PENDING_TIME)
    if raw_time:
        try:
            preferred_time = datetime.fromisoformat(str(raw_time))
        except ValueError:  # pragma: no cover - context is written by us
            logger.warning("Unparseable pending time in context", extra={"raw": raw_time})

    # For a support lead the topic is more useful to the agent than the course.
    topic = conversation.get_ctx(CTX_SUPPORT_TOPIC)
    course_name: str | None = None
    if lead_type is LeadType.POST_SALES and topic:
        course_name = next(
            (title for oid, title, _, _ in copy.SUPPORT_OPTIONS if oid == topic), None
        )
    elif conversation.current_course:
        course = ctx.deps.knowledge_base.get_course(conversation.current_course)
        course_name = course.name if course else conversation.current_course

    rescheduling = conversation.get_ctx(CTX_RESCHEDULE_LEAD_ID)
    if rescheduling and preferred_time is not None:
        moved = await _apply_reschedule(ctx, int(rescheduling), preferred_time)
        if moved is not None:
            return moved

    lead = await ctx.deps.lead_service.create_lead(
        user_id=user.id,
        conversation_id=conversation.id,
        lead_type=lead_type,
        phone=user.phone,
        name=name,
        interested_course=course_name,
        # Already timezone-aware (business timezone); the column stores the
        # absolute instant, so no conversion is needed here.
        preferred_time=preferred_time,
        preferred_time_raw=conversation.get_ctx(CTX_PENDING_TIME_RAW),
        remarks=remarks,
        contact_phone=conversation.get_ctx(CTX_CONTACT_PHONE) or user.phone,
        email=conversation.get_ctx(CTX_EMAIL),
        enrolled_course=conversation.get_ctx(CTX_ENROLLED_COURSE),
        profession=conversation.get_ctx(CTX_PROFILE),
        issue_type=_issue_label(conversation),
    )

    when = (
        CallbackSlot(at=preferred_time, raw="").display()
        if preferred_time
        else "the time you mentioned"
    )
    template = (
        copy.LEAD_CREATED_POST_SALES
        if lead_type is LeadType.POST_SALES
        else copy.LEAD_CREATED_PRE_SALES
    )
    result.add(
        OutboundMessage(
            text=template.format(
                name=name or "there",
                # The number they asked to be called on, which is not always
                # the one they are messaging from. Confirming the WhatsApp
                # number back to someone who just gave a different one reads
                # as though we ignored them - and it is the detail they would
                # most want to check.
                phone=_format_phone(
                    str(conversation.get_ctx(CTX_CONTACT_PHONE) or user.phone)
                ),
                when=when,
            )
        )
    )

    conversation.current_state = ConversationState.LEAD_CREATED
    conversation.clear_ctx(
        CTX_PENDING_TIME, CTX_PENDING_TIME_RAW, CTX_NAME_ATTEMPTS, CTX_RETURN_STATE
    )
    result.lead_id = lead.id
    result.close_conversation = True
    return result


def _issue_label(conversation: object) -> str | None:
    """Support menu choice as a human label, or None outside post-sales."""
    topic = conversation.get_ctx(CTX_SUPPORT_TOPIC)  # type: ignore[attr-defined]
    return copy.issue_label_for(str(topic)) if topic else None


def _resume_state(ctx: TurnContext) -> ConversationState:
    """Where to go back to when a callback offer is declined."""
    stored = ctx.conversation.get_ctx(CTX_RETURN_STATE)
    if stored:
        try:
            return ConversationState(str(stored))
        except ValueError:
            pass
    if ctx.conversation.lead_type is LeadType.POST_SALES:
        return ConversationState.SUPPORT_QUERY
    if ctx.conversation.current_course:
        return ConversationState.COURSE_QNA
    return ConversationState.GENERAL_QNA


def _format_phone(phone: str) -> str:
    return phone if phone.startswith("+") else f"+{phone}"


def _remembered_date(ctx: TurnContext) -> date | None:
    """The date from a previous, rejected attempt in this same booking."""
    raw = ctx.conversation.get_ctx(CTX_PENDING_DATE)
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _capture_volunteered_name(ctx: TurnContext) -> None:
    """Pick up a name offered while we were asking for a time.

    People answer both questions at once - "my name is Ayush Raj and schedule
    the call for 10 august at 4:30" - and this state only looks for a time, so
    the name was dropped and the lead went out under whatever we had before.

    Only acted on when the message actually announces a name, so an ordinary
    time reply is never mistaken for one.
    """
    text = ctx.text.strip()
    lowered = text.lower()
    marker = next(
        (p for p in ("my name is", "my name's", "name is", "this is", "i am", "i'm")
         if p in lowered),
        None,
    )
    if marker is None:
        return

    # Everything from the marker up to the next clause boundary is the name.
    tail = text[lowered.index(marker) + len(marker):]
    candidate = re.split(r"\b(?:and|,|;|\.|book|schedule|call|for|at)\b", tail, maxsplit=1)[0]
    name = clean_name(candidate)
    if not name:
        return

    ctx.user.name = name
    ctx.conversation.set_ctx(CTX_PENDING_NAME, name)
    # NB: "name" is a reserved LogRecord attribute - passing it in `extra`
    # raises KeyError and kills the turn.
    logger.info("Name captured from a time reply", extra={"captured_name": name})


# --------------------------------------------------------------------------- #
# Rescheduling
# --------------------------------------------------------------------------- #
async def start_reschedule(ctx: TurnContext) -> TurnResult:
    """Move an existing booking, or fall back to making one.

    Looked up by phone number rather than by conversation, because the original
    booking almost always happened in an earlier, now-closed conversation.
    """
    result = TurnResult()
    lead = await ctx.deps.lead_repository.find_upcoming_callback(
        ctx.user.phone, now=ctx.deps.callback_validator.now()
    )

    if lead is None:
        # Nothing to move - treat it as a normal request for a call.
        result.add(OutboundMessage(text=copy.RESCHEDULE_NOTHING_BOOKED))
        capture = start_callback_capture(
            ctx, lead_type=ctx.conversation.lead_type or LeadType.PRE_SALES
        )
        result.replies.extend(capture.replies)
        return result

    return _ask_for_the_new_time(ctx, lead)


async def handle_confirm_reschedule(ctx: TurnContext) -> TurnResult:
    """"Move the existing call, or book another?"."""
    reply_id = ctx.inbound.reply_id
    conversation = ctx.conversation

    if reply_id == copy.RESCHEDULE_NEW or intents.is_negative(ctx.text):
        conversation.clear_ctx(CTX_RESCHEDULE_LEAD_ID)
        conversation.current_state = ConversationState.ASK_CALLBACK_TIME
        name = str(conversation.get_ctx(CTX_PENDING_NAME) or ctx.user.display_name)
        result = TurnResult()
        result.add(OutboundMessage(text=ask_time_text(ctx, name)))
        return result

    if reply_id == copy.RESCHEDULE_MOVE or intents.is_affirmative(ctx.text):
        lead = await ctx.deps.lead_repository.find_upcoming_callback(
        ctx.user.phone, now=ctx.deps.callback_validator.now()
    )
        if lead is not None:
            return _ask_for_the_new_time(ctx, lead)

    # Anything else: re-ask rather than guess which they meant.
    result = TurnResult()
    lead = await ctx.deps.lead_repository.find_upcoming_callback(
        ctx.user.phone, now=ctx.deps.callback_validator.now()
    )
    when = describe_slot(lead, ctx.deps.settings.tz) if lead else "your call"
    result.add(copy.reschedule_choice(when))
    return result


def _ask_for_the_new_time(ctx: TurnContext, lead: Lead) -> TurnResult:
    conversation = ctx.conversation
    conversation.set_ctx(CTX_RESCHEDULE_LEAD_ID, lead.id)
    conversation.clear_ctx(CTX_PENDING_DATE)
    conversation.lead_type = lead.type
    conversation.current_state = ConversationState.ASK_CALLBACK_TIME

    result = TurnResult()
    result.add(
        OutboundMessage(
            text=copy.RESCHEDULE_PROMPT.format(
                when=describe_slot(lead, ctx.deps.settings.tz),
                hours=ctx.deps.callback_validator.business_hours_text(),
            )
        )
    )
    return result


async def _apply_reschedule(
    ctx: TurnContext, lead_id: int, preferred_time: datetime
) -> TurnResult | None:
    """Move the existing booking. Returns None if it can no longer be found.

    Falling back to creating a new lead is deliberate: if the original was
    deleted or already worked, losing the request entirely would be worse than
    an extra row.
    """
    conversation = ctx.conversation
    lead = await ctx.deps.lead_repository.get_by_id(lead_id)
    if lead is None or lead.phone != ctx.user.phone:
        logger.warning("Reschedule target vanished", extra={"lead_id": lead_id})
        conversation.clear_ctx(CTX_RESCHEDULE_LEAD_ID)
        return None

    await ctx.deps.lead_repository.reschedule(
        lead,
        preferred_time=preferred_time,
        preferred_time_raw=conversation.get_ctx(CTX_PENDING_TIME_RAW),
    )
    conversation.clear_ctx(CTX_RESCHEDULE_LEAD_ID)
    conversation.current_state = ConversationState.END

    logger.info(
        "Callback rescheduled",
        extra={"lead_id": lead.id, "new_time": preferred_time.isoformat()},
    )

    result = TurnResult()
    result.add(
        OutboundMessage(
            text=copy.RESCHEDULE_DONE.format(
                when=CallbackSlot(at=preferred_time, raw="").display()
            )
        )
    )
    result.lead_id = lead.id
    result.close_conversation = True
    return result


def describe_slot(lead: Lead, tz: ZoneInfo) -> str:
    """Human-readable booked time, in the business timezone.

    The column stores the absolute instant, so a value read back from the
    database arrives in UTC. Formatting it directly told a user their 4 PM call
    was at 10:30 AM - correct to the second, and useless.

    `preferred_time` is nullable on the model, and although
    `find_upcoming_callback` only returns rows that have one, a lead reached by
    id might not - so this degrades to a phrase rather than raising mid-turn.
    """
    if lead.preferred_time is None:
        return "your upcoming call"
    return CallbackSlot(at=lead.preferred_time.astimezone(tz), raw="").display()
