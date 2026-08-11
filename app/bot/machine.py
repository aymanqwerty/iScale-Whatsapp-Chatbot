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
from app.bot.handlers.callback import describe_slot, start_reschedule
from app.bot.handlers.common import start_callback_capture
from app.bot.handlers.discovery import start_discovery
from app.bot.handlers.offer import handle_offer_reply
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

        override = await self._global_command(ctx)
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
    async def _global_command(self, ctx: TurnContext) -> TurnResult | None:
        """Commands that work from (almost) any state.

        This is what makes the bot predictable. Anything resolved here never
        reaches the model, so "hi" cannot come back as improvised prose and
        "what courses do you have" cannot come back as one course out of eight.
        The model answers questions; it never decides where the conversation is.

        Deliberately suppressed during callback capture: a user answering the
        name question with "Menu" is far less likely than one whose free-text
        note happens to contain a trigger word, and being thrown out of the form
        halfway through is the worse failure.
        """
        conversation = ctx.conversation
        state = conversation.current_state
        text = ctx.text

        # Offer buttons first: they are answers to the discount message, not
        # navigation, and "Talk to a Counselor" on that card deliberately falls
        # through to the shared escalation path below.
        offer_reply = handle_offer_reply(ctx)
        if offer_reply is not None:
            return offer_reply

        navigation = self._tapped_navigation(ctx)
        if navigation is not None:
            return navigation

        # Any other tapped button answers the question just asked.
        if ctx.inbound.reply_id:
            return None

        in_capture = state in CALLBACK_CAPTURE_STATES

        if intents.wants_menu(text) and not in_capture:
            self._reset_flow(ctx)
            conversation.current_state = ConversationState.MAIN_MENU
            result = TurnResult()
            result.add(copy.main_menu(copy.SESSION_RESTART))
            return result

        # A greeting is an opener, not a question. Answering it with the menu
        # gives every conversation the same predictable starting point, however
        # far into the flow the user has wandered.
        if intents.is_greeting(text) and not in_capture:
            self._reset_flow(ctx)
            conversation.current_state = ConversationState.MAIN_MENU
            result = TurnResult()
            result.add(
                copy.welcome_message(ctx.company, returning_name=ctx.user.name)
            )
            return result

        asks_for_catalogue = intents.wants_course_list(
            text, course_selected=conversation.current_course is not None
        )
        if asks_for_catalogue and not in_capture:
            conversation.current_state = ConversationState.COURSE_GROUP
            conversation.lead_type = conversation.lead_type or LeadType.PRE_SALES
            result = TurnResult()
            result.add(copy.course_group_menu())
            return result

        # Checked before `wants_human`, which would otherwise start a fresh
        # capture and leave the original call still on the counselor's list.
        if intents.wants_reschedule(text) and not in_capture:
            logger.info("Reschedule requested", extra={"state": str(state)})
            return await start_reschedule(ctx)

        if (
            intents.wants_human(text)
            and not in_capture
            and state
            not in (
                ConversationState.ASK_CALLBACK,
                ConversationState.SUPPORT_CALLBACK,
                ConversationState.CONFIRM_RESCHEDULE,
            )
        ):
            lead_type = conversation.lead_type or LeadType.PRE_SALES
            logger.info("Escalation requested by user", extra={"state": str(state)})
            return await self._start_or_offer_move(ctx, lead_type)

        return None

    async def _start_or_offer_move(
        self, ctx: TurnContext, lead_type: LeadType
    ) -> TurnResult:
        """Begin a booking, unless one is already on the books.

        Booking twice is nearly always an accident - the user forgot, or thought
        the first attempt had failed. Sending a counselor to ring the same
        person twice wastes their time and looks careless, so we ask.
        """
        existing = await ctx.deps.lead_repository.find_upcoming_callback(
        ctx.user.phone, now=ctx.deps.callback_validator.now()
    )
        if existing is None:
            return start_callback_capture(ctx, lead_type=lead_type)

        ctx.conversation.current_state = ConversationState.CONFIRM_RESCHEDULE
        result = TurnResult()
        result.add(copy.reschedule_choice(describe_slot(existing, ctx.deps.settings.tz)))
        return result

    def _tapped_navigation(self, ctx: TurnContext) -> TurnResult | None:
        """Honour a tapped navigation button from any state.

        WhatsApp messages never expire, so a user can scroll up and tap a menu
        row sent twenty minutes ago. Previously every tap was treated as an
        answer to the *current* question, so tapping "Not Enrolled Yet" during
        Q&A sent the row's title to the model as free text - which answered a
        question nobody asked and then offered a callback.

        Only navigation ids are handled here. `confirm:yes`, `support:*` and the
        like are answers to a specific question and must stay with the handler
        that asked it.
        """
        reply_id = ctx.inbound.reply_id
        if not reply_id:
            return None

        conversation = ctx.conversation
        if conversation.current_state in CALLBACK_CAPTURE_STATES:
            # Mid-form: a stray tap must not discard a half-captured booking.
            return None

        if reply_id == copy.MENU_COURSES:
            self._reset_flow(ctx)
            conversation.current_state = ConversationState.COURSE_GROUP
            conversation.lead_type = conversation.lead_type or LeadType.PRE_SALES
            result = TurnResult()
            result.add(copy.course_group_menu())
            return result

        if reply_id in (copy.GROUP_COHORT, copy.GROUP_ADVANCE):
            group = reply_id.removeprefix(copy.GROUP_PREFIX)
            conversation.current_state = ConversationState.COURSE_SELECTION
            conversation.lead_type = conversation.lead_type or LeadType.PRE_SALES
            result = TurnResult()
            result.add(copy.course_group_submenu(ctx.deps.knowledge_base, group))
            return result

        if reply_id == copy.COURSE_UNSURE:
            return start_discovery(ctx)

        if reply_id == copy.MENU_ENROLLED:
            self._reset_flow(ctx)
            conversation.current_state = ConversationState.ENROLLMENT_TYPE
            # Left unset until they say free or paid: a free-course student is
            # a pre-sales lead, so committing to POST_SALES here would misfile
            # every one of them.
            result = TurnResult()
            result.add(copy.enrollment_type_menu())
            return result

        if reply_id == copy.ENROLLED_PAID:
            conversation.current_state = ConversationState.POST_SALES
            conversation.lead_type = LeadType.POST_SALES
            result = TurnResult()
            result.add(copy.support_menu())
            return result

        if reply_id == copy.ENROLLED_FREE:
            # No callback for free courses - but they have already shown intent,
            # so they go into discovery rather than being turned away.
            return start_discovery(ctx, opener=copy.FREE_COURSE_NO_SUPPORT)

        if reply_id == copy.MENU_COUNSELOR:
            lead_type = conversation.lead_type or LeadType.PRE_SALES
            return start_callback_capture(ctx, lead_type=lead_type)

        if reply_id == copy.COURSE_OTHERS:
            conversation.current_state = ConversationState.COURSE_SELECTION
            result = TurnResult()
            result.add(copy.other_courses_menu(ctx.deps.knowledge_base))
            return result

        # A course row: select it wherever the user happens to be.
        if reply_id.startswith(copy.COURSE_PREFIX) and reply_id != copy.COURSE_UNSURE:
            slug = reply_id.removeprefix(copy.COURSE_PREFIX)
            course = ctx.deps.knowledge_base.get_course(slug)
            if course is not None:
                conversation.current_state = ConversationState.COURSE_QNA
                conversation.current_course = course.slug
                conversation.lead_type = conversation.lead_type or LeadType.PRE_SALES
                result = TurnResult()
                result.add(
                    OutboundMessage(
                        text=copy.COURSE_SELECTED.format(
                            course=course.name, summary=course.summary_line()
                        )
                    )
                )
                return result

        return None

    @staticmethod
    def _reset_flow(ctx: TurnContext) -> None:
        """Clear per-branch scratch state when returning to the main menu."""
        conversation = ctx.conversation
        conversation.current_course = None
        conversation.clear_ctx(CTX_QNA_COUNT, CTX_NUDGED)
