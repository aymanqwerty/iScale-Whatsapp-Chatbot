"""Helpers shared by the state handlers."""

from __future__ import annotations

import re

from app.bot import copy
from app.bot.context import (
    CTX_PENDING_NAME,
    CTX_PROFILE,
    CTX_RETURN_STATE,
    TurnContext,
)
from app.core.logging import get_logger
from app.domain.enums import ConversationState, LeadType
from app.domain.messaging import OutboundMessage, TurnResult
from app.services.knowledge.models import Audience
from app.services.llm.answer_service import AnswerRequest

logger = get_logger(__name__)

#: Lead-ins people put before their name, stripped before storing it.
_NAME_PREFIXES = (
    "my name is", "my name's", "name is", "this is", "i am", "i'm", "im",
    "myself", "call me", "its", "it's",
)

_NAME_ALLOWED = re.compile(r"^[a-zA-Zऀ-ॿ][a-zA-Zऀ-ॿ\s.'\-]{1,59}$")


def audience_for(state: ConversationState, lead_type: LeadType | None) -> Audience:
    """Restrict retrieval to content meant for this side of the funnel."""
    if lead_type is LeadType.POST_SALES or state in (
        ConversationState.POST_SALES,
        ConversationState.SUPPORT_QUERY,
        ConversationState.SUPPORT_CALLBACK,
    ):
        return "post_sales"
    return "pre_sales"


async def answer_question(
    ctx: TurnContext,
    *,
    question: str | None = None,
    nudge_callback: bool = False,
    course_slug: str | None = None,
) -> str:
    """Run one grounded LLM answer for the current turn.

    `course_slug` overrides what retrieval is scoped to. Discovery uses it to
    ground the pitch in the upsell course even though the user has not selected
    anything, which is the difference between arguing from real course facts and
    improvising.
    """
    conversation = ctx.conversation
    profile = str(conversation.get_ctx(CTX_PROFILE, "") or "").strip()
    request = AnswerRequest(
        question=question or ctx.text,
        state=conversation.current_state,
        course_slug=course_slug or conversation.current_course,
        audience=audience_for(conversation.current_state, conversation.lead_type),
        history=ctx.history,
        nudge_callback=nudge_callback,
        known_profile=profile or None,
    )
    result = await ctx.deps.answer_service.answer(request)
    return result.text


async def answer_with_optional_nudge(ctx: TurnContext) -> TurnResult:
    """Answer a question and, at the right moment, offer a callback.

    The offer is a separate interactive message rather than a sentence the model
    is asked to append: buttons are unambiguous, and the transition into
    `ASK_CALLBACK` has to happen whether or not the model cooperated.
    """
    result = TurnResult()
    resume_state = ctx.conversation.current_state

    text = await answer_question(ctx)
    result.add(OutboundMessage(text=text))

    ctx.bump_qna_count()

    if ctx.should_nudge_callback():
        ctx.mark_nudged()
        ctx.conversation.set_ctx(CTX_RETURN_STATE, str(resume_state))
        result.add(_callback_offer(ctx))
        ctx.conversation.current_state = ConversationState.ASK_CALLBACK

    return result


def _callback_offer(ctx: TurnContext) -> OutboundMessage:
    is_support = ctx.conversation.lead_type is LeadType.POST_SALES
    prompt = (
        copy.ASK_CALLBACK_POST_SALES if is_support else copy.ASK_CALLBACK_PRE_SALES
    )
    return copy.yes_no(prompt)


def offer_callback(ctx: TurnContext, *, resume_state: ConversationState) -> TurnResult:
    """Explicitly ask whether the user wants a callback."""
    ctx.mark_nudged()
    ctx.conversation.set_ctx(CTX_RETURN_STATE, str(resume_state))
    ctx.conversation.current_state = ConversationState.ASK_CALLBACK
    result = TurnResult()
    result.add(_callback_offer(ctx))
    return result


def start_callback_capture(ctx: TurnContext, *, lead_type: LeadType) -> TurnResult:
    """Enter the name / time / remarks capture sequence.

    A returning user whose name we already hold skips straight to the time
    question - asking a known contact for their name again reads as amnesia.
    """
    from app.bot.handlers.capture import next_question

    conversation = ctx.conversation
    conversation.lead_type = lead_type

    known_name = ctx.user.name
    if known_name:
        conversation.set_ctx(CTX_PENDING_NAME, known_name)

    # Ask for the first thing still missing rather than a fixed first question.
    # For a returning pre-sales contact whose name we hold, that is the phone
    # confirmation; for a new post-sales one it is the name.
    staged = next_question(ctx)
    if staged is not None:
        return staged

    result = TurnResult()
    if known_name:
        conversation.current_state = ConversationState.ASK_CALLBACK_TIME
        result.add(OutboundMessage(text=ask_time_text(ctx, known_name)))
    else:
        conversation.current_state = ConversationState.ASK_NAME
        result.add(OutboundMessage(text=copy.ASK_NAME))

    return result


def ask_time_text(ctx: TurnContext, name: str) -> str:
    return copy.ASK_CALLBACK_TIME.format(
        name=name,
        hours=ctx.deps.callback_validator.business_hours_text(),
    )


def clean_name(raw: str) -> str | None:
    """Extract a usable name, or None if the message clearly is not one."""
    text = " ".join(raw.strip().split())
    if not text:
        return None

    lowered = text.lower()
    for prefix in _NAME_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip(" .,:-")
            break

    text = text.strip(" .,:-")
    if not text or len(text) > 60:
        return None
    # Questions and messages with digits are not names.
    if "?" in text or any(char.isdigit() for char in text):
        return None
    if not _NAME_ALLOWED.match(text):
        return None
    if len(text.replace(" ", "")) < 2:
        return None

    return text.title() if text.islower() or text.isupper() else text


def unsupported_reply() -> TurnResult:
    result = TurnResult()
    result.add(OutboundMessage(text=copy.UNSUPPORTED_MESSAGE))
    return result
