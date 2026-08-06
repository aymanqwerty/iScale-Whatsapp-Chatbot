"""Callback-capture handlers: consent, name, time, remarks, lead creation."""

from __future__ import annotations

from datetime import datetime

from app.bot import copy, intents
from app.bot.context import (
    CTX_NAME_ATTEMPTS,
    CTX_PENDING_NAME,
    CTX_PENDING_TIME,
    CTX_PENDING_TIME_RAW,
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
from app.domain.enums import ConversationState, LeadType
from app.domain.messaging import OutboundMessage, TurnResult
from app.services.scheduling.callback_time import CallbackSlot

logger = get_logger(__name__)

#: After this many unusable replies we stop asking and accept whatever was sent,
#: rather than trapping the user in a loop over a name field.
_MAX_NAME_ATTEMPTS = 2


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


async def handle_ask_name(ctx: TurnContext) -> TurnResult:
    """Capture the user's name."""
    result = TurnResult()
    conversation = ctx.conversation

    name = clean_name(ctx.text)
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
    conversation.current_state = ConversationState.ASK_CALLBACK_TIME

    result.add(OutboundMessage(text=ask_time_text(ctx, name)))
    return result


async def handle_ask_callback_time(ctx: TurnContext) -> TurnResult:
    """Parse and validate the requested callback slot."""
    result = TurnResult()
    conversation = ctx.conversation
    validator = ctx.deps.callback_validator

    parsed = validator.parse(ctx.text)

    if not parsed.ok:
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

    slot: CallbackSlot = parsed.slot  # type: ignore[assignment]
    conversation.update_ctx(
        **{
            CTX_PENDING_TIME: slot.at.isoformat(),
            CTX_PENDING_TIME_RAW: ctx.text.strip()[:255],
        }
    )
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
                name=name or "there", phone=_format_phone(user.phone), when=when
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
