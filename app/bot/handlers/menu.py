"""START and MAIN_MENU handlers."""

from __future__ import annotations

from app.bot import copy, intents
from app.bot.context import TurnContext
from app.bot.handlers.common import (
    answer_question,
    start_callback_capture,
)
from app.domain.enums import ConversationState, LeadType
from app.domain.messaging import OutboundMessage, TurnResult


async def handle_start(ctx: TurnContext) -> TurnResult:
    """First message of a conversation: greet and show the menu.

    A returning contact is greeted by name, and a first message that already
    states an intent ("do you have weekend batches?") is not forced back through
    the menu - it falls through to MAIN_MENU, which answers it.
    """
    result = TurnResult()
    ctx.conversation.current_state = ConversationState.MAIN_MENU

    if intents.is_greeting(ctx.text) or not ctx.text:
        result.add(
            copy.welcome_message(ctx.company, returning_name=ctx.user.name)
        )
        return result

    # Anything more substantial than "hi": welcome briefly, then deal with it.
    result.add(copy.welcome_message(ctx.company, returning_name=ctx.user.name))
    follow_up = await handle_main_menu(ctx)
    result.replies.extend(follow_up.replies)
    result.lead_id = follow_up.lead_id
    result.close_conversation = follow_up.close_conversation
    return result


async def handle_main_menu(ctx: TurnContext) -> TurnResult:
    """Route a menu choice, or answer a question typed instead of choosing."""
    result = TurnResult()
    conversation = ctx.conversation

    choice = intents.match_option(ctx.inbound, copy.MAIN_MENU_OPTIONS)

    if choice == copy.MENU_COURSES:
        conversation.current_state = ConversationState.COURSE_SELECTION
        conversation.lead_type = LeadType.PRE_SALES
        result.add(copy.course_menu())
        return result

    if choice == copy.MENU_ENROLLED:
        conversation.current_state = ConversationState.POST_SALES
        conversation.lead_type = LeadType.POST_SALES
        result.add(OutboundMessage(text=copy.POST_SALES_INTRO))
        result.add(copy.support_menu())
        return result

    if choice == copy.MENU_COUNSELOR:
        capture = start_callback_capture(ctx, lead_type=LeadType.PRE_SALES)
        result.replies.extend(capture.replies)
        return result

    if choice == copy.MENU_GENERAL:
        conversation.current_state = ConversationState.GENERAL_QNA
        conversation.lead_type = conversation.lead_type or LeadType.PRE_SALES
        result.add(OutboundMessage(text=copy.GENERAL_QNA_INTRO))
        return result

    # No option matched. If the message names a course, jump straight into it.
    course = ctx.deps.knowledge_base.match_course(ctx.text)
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

    if intents.is_greeting(ctx.text):
        result.add(copy.welcome_message(ctx.company, returning_name=ctx.user.name))
        return result

    # A real question typed at the menu: answer it rather than nagging for a tap.
    conversation.current_state = ConversationState.GENERAL_QNA
    conversation.lead_type = conversation.lead_type or LeadType.PRE_SALES
    answer = await answer_question(ctx)
    ctx.bump_qna_count()
    result.add(OutboundMessage(text=answer))
    result.add(copy.main_menu("Anything else I can help with?"))
    return result
