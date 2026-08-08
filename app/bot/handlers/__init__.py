"""State handlers, registered against the states they serve.

One handler per state, each a coroutine `(TurnContext) -> TurnResult`. Adding a
state means adding a handler and one entry in this table - the dispatcher itself
never changes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.bot.context import TurnContext
from app.bot.handlers.callback import (
    handle_ask_callback,
    handle_ask_callback_time,
    handle_ask_name,
    handle_ask_remarks,
    handle_confirm_reschedule,
    handle_end,
    handle_lead_created,
)
from app.bot.handlers.courses import (
    handle_course_qna,
    handle_course_selection,
    handle_general_qna,
)
from app.bot.handlers.menu import handle_main_menu, handle_start
from app.bot.handlers.support import (
    handle_post_sales,
    handle_support_callback,
    handle_support_query,
)
from app.domain.enums import ConversationState
from app.domain.messaging import TurnResult

StateHandler = Callable[[TurnContext], Awaitable[TurnResult]]

HANDLERS: dict[ConversationState, StateHandler] = {
    ConversationState.START: handle_start,
    ConversationState.MAIN_MENU: handle_main_menu,
    ConversationState.COURSE_SELECTION: handle_course_selection,
    ConversationState.COURSE_QNA: handle_course_qna,
    ConversationState.GENERAL_QNA: handle_general_qna,
    ConversationState.POST_SALES: handle_post_sales,
    ConversationState.SUPPORT_QUERY: handle_support_query,
    ConversationState.SUPPORT_CALLBACK: handle_support_callback,
    ConversationState.CONFIRM_RESCHEDULE: handle_confirm_reschedule,
    ConversationState.ASK_CALLBACK: handle_ask_callback,
    ConversationState.ASK_NAME: handle_ask_name,
    ConversationState.ASK_CALLBACK_TIME: handle_ask_callback_time,
    ConversationState.ASK_REMARKS: handle_ask_remarks,
    ConversationState.LEAD_CREATED: handle_lead_created,
    ConversationState.END: handle_end,
}

__all__ = ["HANDLERS", "StateHandler"]
