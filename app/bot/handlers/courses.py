"""Pre-sales handlers: course selection and course / general Q&A."""

from __future__ import annotations

from app.bot import copy, intents
from app.bot.context import TurnContext
from app.bot.handlers.common import (
    answer_question,
    answer_with_optional_nudge,
)
from app.domain.enums import ConversationState, LeadType
from app.domain.messaging import OutboundMessage, TurnResult


async def handle_course_selection(ctx: TurnContext) -> TurnResult:
    """Pick a course from the list, by tap, name or number."""
    result = TurnResult()
    conversation = ctx.conversation
    knowledge_base = ctx.deps.knowledge_base

    reply_id = ctx.inbound.reply_id or ""

    if reply_id == copy.COURSE_UNSURE or "not sure" in intents.normalize(ctx.text):
        conversation.current_state = ConversationState.GENERAL_QNA
        result.add(OutboundMessage(text=copy.UNSURE_COURSE))
        return result

    # "Other courses" opens the second-level list and stays in this state, so
    # the next tap is handled by exactly the same branch below.
    if reply_id == copy.COURSE_OTHERS:
        result.add(copy.other_courses_menu(knowledge_base))
        return result

    slug: str | None = None
    if reply_id.startswith(copy.COURSE_PREFIX):
        slug = reply_id.removeprefix(copy.COURSE_PREFIX)

    course = knowledge_base.get_course(slug) or knowledge_base.match_course(ctx.text)

    # A positional reply ("3") maps onto the order the menu was rendered in -
    # which is the featured list, not every course we know about.
    if course is None:
        cleaned = intents.normalize(ctx.text)
        if cleaned.isdigit():
            index = int(cleaned) - 1
            courses = knowledge_base.featured_courses
            if 0 <= index < len(courses):
                course = courses[index]

    if course is not None:
        conversation.current_state = ConversationState.COURSE_QNA
        conversation.current_course = course.slug
        conversation.lead_type = LeadType.PRE_SALES
        result.add(
            OutboundMessage(
                text=copy.COURSE_SELECTED.format(
                    course=course.name, summary=course.summary_line()
                )
            )
        )
        return result

    # Not a course name - most likely a general question asked at the menu.
    if len(ctx.text.split()) >= 3:
        conversation.current_state = ConversationState.GENERAL_QNA
        answer = await answer_question(ctx)
        ctx.bump_qna_count()
        result.add(OutboundMessage(text=answer))
        return result

    result.add(OutboundMessage(text=copy.FALLBACK_UNRECOGNISED))
    result.add(copy.course_menu(knowledge_base))
    return result


async def handle_course_qna(ctx: TurnContext) -> TurnResult:
    """Unlimited questions about the selected course."""
    # Switching course mid-conversation is common ("what about data science?").
    course = ctx.deps.knowledge_base.match_course(ctx.text)
    if course is not None and course.slug != ctx.conversation.current_course:
        ctx.conversation.current_course = course.slug

    return await answer_with_optional_nudge(ctx)


async def handle_general_qna(ctx: TurnContext) -> TurnResult:
    """Career advice, comparisons, roadmaps - anything not course-scoped."""
    course = ctx.deps.knowledge_base.match_course(ctx.text)
    if course is not None:
        # The user has settled on a course; narrow the retrieval scope to it.
        ctx.conversation.current_course = course.slug
        ctx.conversation.current_state = ConversationState.COURSE_QNA

    return await answer_with_optional_nudge(ctx)
