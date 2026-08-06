"""The conversation state machine.

Flow control lives here and in the handlers - never in the LLM. The model is
asked to write prose; it is never asked where the conversation is or what
happens next. That is what makes the funnel reliable: a lead is created because
the machine reached `ASK_REMARKS` with a validated slot, not because a model
decided it had gathered enough.
"""

from __future__ import annotations

from app.bot import copy, intents
from app.bot.context import CTX_NUDGED, CTX_QNA_COUNT, TurnContext
from app.bot.handlers import HANDLERS
from app.bot.handlers.common import start_callback_capture
from app.core.logging import get_logger
from app.domain.enums import (
    CALLBACK_CAPTURE_STATES,
    ConversationState,
    LeadType,
    MessageKind,
)
from app.domain.messaging import OutboundMessage, TurnResult

logger = get_logger(__name__)


class ConversationMachine:
    """Applies one inbound message to one conversation."""

    async def handle(self, ctx: TurnContext) -> TurnResult:
        conversation = ctx.conversation
        state = conversation.current_state

        if ctx.inbound.kind is MessageKind.UNSUPPORTED:
            result = TurnResult()
            result.add(OutboundMessage(text=copy.UNSUPPORTED_MESSAGE))
            return result

        override = self._global_command(ctx)
        if override is not None:
            return override

        handler = HANDLERS.get(state)
        if handler is None:  # pragma: no cover - every state is registered
            logger.error("No handler registered", extra={"state": str(state)})
            conversation.current_state = ConversationState.MAIN_MENU
            handler = HANDLERS[ConversationState.MAIN_MENU]

        result = await handler(ctx)

        logger.info(
            "Turn processed",
            extra={
                "from_state": str(state),
                "to_state": str(conversation.current_state),
                "replies": len(result.replies),
                "lead_id": result.lead_id,
            },
        )
        return result

    # ------------------------------------------------------------------ #
    def _global_command(self, ctx: TurnContext) -> TurnResult | None:
        """Commands that work from (almost) any state.

        Deliberately suppressed during callback capture: a user answering the
        name question with "Menu" is far less likely than one whose free-text
        note happens to contain a trigger word, and being thrown out of the form
        halfway through is the worse failure.
        """
        conversation = ctx.conversation
        state = conversation.current_state
        text = ctx.text

        # A tapped button is an answer to the question just asked, never a command.
        if ctx.inbound.reply_id:
            return None

        in_capture = state in CALLBACK_CAPTURE_STATES

        if intents.wants_menu(text) and not in_capture:
            self._reset_flow(ctx)
            conversation.current_state = ConversationState.MAIN_MENU
            result = TurnResult()
            result.add(copy.main_menu(copy.SESSION_RESTART))
            return result

        if (
            intents.wants_human(text)
            and not in_capture
            and state
            not in (ConversationState.ASK_CALLBACK, ConversationState.SUPPORT_CALLBACK)
        ):
            lead_type = conversation.lead_type or LeadType.PRE_SALES
            logger.info("Escalation requested by user", extra={"state": str(state)})
            return start_callback_capture(ctx, lead_type=lead_type)

        return None

    @staticmethod
    def _reset_flow(ctx: TurnContext) -> None:
        """Clear per-branch scratch state when returning to the main menu."""
        conversation = ctx.conversation
        conversation.current_course = None
        conversation.clear_ctx(CTX_QNA_COUNT, CTX_NUDGED)
