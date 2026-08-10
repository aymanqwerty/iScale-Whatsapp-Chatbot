"""Post-sales handlers: enrolled-student support."""

from __future__ import annotations

from app.bot import copy, intents
from app.bot.context import CTX_ENROLLED_COURSE, CTX_SUPPORT_TOPIC, TurnContext
from app.bot.handlers.common import (
    answer_question,
    offer_callback,
    start_callback_capture,
)
from app.bot.handlers.discovery import start_discovery
from app.domain.enums import ConversationState, LeadType
from app.domain.messaging import OutboundMessage, TurnResult

#: Topics that always need a human - account changes, payments, anything
#: requiring identity verification. The bot explains, then offers the callback
#: rather than pretending it can act.
_ALWAYS_ESCALATE = frozenset({f"{copy.SUPPORT_PREFIX}other"})


async def handle_enrollment_type(ctx: TurnContext) -> TurnResult:
    """Free course or paid course - asked before anything else.

    The split has to come first because the two answers lead to opposite places:
    a paid student goes into verified post-sales capture, a free-course student
    is told plainly that free courses carry no support and is then treated as
    the warm pre-sales lead they actually are.
    """
    result = TurnResult()
    conversation = ctx.conversation
    reply_id = ctx.inbound.reply_id or ""
    text = intents.normalize(ctx.text)

    paid = reply_id == copy.ENROLLED_PAID or "paid" in text
    free = reply_id == copy.ENROLLED_FREE or "free" in text

    # "free" appears inside phrases about paid courses far less often than the
    # reverse, but check paid first so "I paid for the free trial" is not free.
    if paid:
        conversation.current_state = ConversationState.POST_SALES
        conversation.lead_type = LeadType.POST_SALES
        result.add(copy.support_menu())
        return result

    if free:
        return start_discovery(ctx, opener=copy.FREE_COURSE_NO_SUPPORT)

    # Naming their course answers the question without answering it.
    course = ctx.deps.knowledge_base.match_course(ctx.text)
    if course is not None:
        conversation.set_ctx(CTX_ENROLLED_COURSE, course.name)
        if course.group == "free":
            return start_discovery(ctx, opener=copy.FREE_COURSE_NO_SUPPORT)
        conversation.current_state = ConversationState.POST_SALES
        conversation.lead_type = LeadType.POST_SALES
        result.add(copy.support_menu())
        return result

    result.add(OutboundMessage(text=copy.FALLBACK_UNRECOGNISED))
    result.add(copy.enrollment_type_menu())
    return result


async def handle_post_sales(ctx: TurnContext) -> TurnResult:
    """Choose a support topic."""
    result = TurnResult()
    conversation = ctx.conversation
    conversation.lead_type = LeadType.POST_SALES

    topic = intents.match_option(ctx.inbound, copy.SUPPORT_OPTIONS)

    if topic is None:
        # Free text straight away - treat it as the issue itself.
        if len(ctx.text.split()) >= 3:
            conversation.current_state = ConversationState.SUPPORT_QUERY
            return await handle_support_query(ctx)
        result.add(OutboundMessage(text=copy.FALLBACK_UNRECOGNISED))
        result.add(copy.support_menu())
        return result

    conversation.set_ctx(CTX_SUPPORT_TOPIC, topic)
    conversation.current_state = ConversationState.SUPPORT_QUERY

    if topic in _ALWAYS_ESCALATE:
        result.add(
            OutboundMessage(
                text="Sure - tell me briefly what you need, and I'll pass it to the "
                "support team."
            )
        )
        return result

    label = next(
        (title for oid, title, _, _ in copy.SUPPORT_OPTIONS if oid == topic), "that"
    )
    result.add(
        OutboundMessage(
            text=f"Okay, {label.lower()}. Could you describe the issue in a line or two "
            "so I can help properly?"
        )
    )
    return result


async def handle_support_query(ctx: TurnContext) -> TurnResult:
    """Answer the support question, then check whether it is resolved.

    Support has a lower nudge threshold than sales on purpose: a student with an
    unresolved problem should reach a human quickly, not after a fixed number of
    questions.
    """
    result = TurnResult()
    conversation = ctx.conversation

    if intents.wants_human(ctx.text):
        capture = start_callback_capture(ctx, lead_type=LeadType.POST_SALES)
        result.replies.extend(capture.replies)
        return result

    answer = await answer_question(ctx)
    result.add(OutboundMessage(text=answer))
    ctx.bump_qna_count()

    conversation.current_state = ConversationState.SUPPORT_CALLBACK
    result.add(
        copy.yes_no(
            "Did that help, or would you like our support team to call you?",
            yes_label="That helped",
            no_label="Please call me",
        )
    )
    return result


async def handle_support_callback(ctx: TurnContext) -> TurnResult:
    """Interpret the "did that help?" reply.

    Note the inverted buttons: "That helped" is the *yes* id but means no
    callback, while "Please call me" is the *no* id and means escalate.
    """
    result = TurnResult()
    conversation = ctx.conversation
    reply_id = ctx.inbound.reply_id

    resolved = reply_id == copy.CONFIRM_YES or (
        reply_id is None and intents.is_affirmative(ctx.text)
    )
    escalate = reply_id == copy.CONFIRM_NO or (
        reply_id is None and (intents.is_negative(ctx.text) or intents.wants_human(ctx.text))
    )

    if escalate:
        capture = start_callback_capture(ctx, lead_type=LeadType.POST_SALES)
        result.replies.extend(capture.replies)
        return result

    if resolved:
        conversation.current_state = ConversationState.SUPPORT_QUERY
        result.add(
            OutboundMessage(
                text="Glad that helped! 🙂 Anything else I can look into for you?"
            )
        )
        return result

    # Neither - the user asked a follow-up question instead of answering.
    conversation.current_state = ConversationState.SUPPORT_QUERY
    return await handle_support_query(ctx)


__all__ = [
    "handle_enrollment_type",
    "handle_post_sales",
    "handle_support_callback",
    "handle_support_query",
    "offer_callback",
]
