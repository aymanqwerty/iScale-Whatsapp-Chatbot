"""The chatbot-exclusive discount: when it is shown, and what follows.

The offer exists to close a conversation that is already going well. Deciding
*when* is the whole job, and it is done here rather than by the model for two
reasons: a model shown a coupon will mention it in the first reply to anybody,
which trains people to ignore it; and a model asked to describe a discount will
eventually get the number wrong.

So: the machine decides the moment, `copy.offer_message` renders the exact
figures from `offers.json`, and the model never sees either.
"""

from __future__ import annotations

from app.bot import copy, intents
from app.bot.context import CTX_OFFER_SENT, TurnContext
from app.core.logging import get_logger
from app.domain.enums import ConversationState, LeadType
from app.domain.messaging import OutboundMessage, TurnResult

logger = get_logger(__name__)

#: States where a discount is a sensible thing to raise. Deliberately excludes
#: every post-sales state - offering a course discount to someone chasing a
#: broken video is tone deaf - and excludes capture, where a booking is already
#: half collected and interrupting it would lose the lead.
_SELLING_STATES: frozenset[ConversationState] = frozenset(
    {
        ConversationState.DISCOVERY,
        ConversationState.COURSE_QNA,
        ConversationState.GENERAL_QNA,
    }
)


def offer_is_live(ctx: TurnContext) -> bool:
    """Whether there is an enabled offer we have not already shown."""
    if not ctx.deps.knowledge_base.offer:
        return False
    return not ctx.conversation.get_ctx(CTX_OFFER_SENT, False)


def _relevant(ctx: TurnContext) -> bool:
    """True only when the conversation is actually about the discounted course.

    The rule is the same everywhere: the offer needs `current_course` to be the
    discounted course, or unset. Discovery is NOT a blanket exemption - a user
    can steer discovery onto another course ("tell me about the ML program")
    while the state stays DISCOVERY, and treating that as eligible dangled an
    AI For Everyone coupon at someone reading about Machine Learning with
    Agentic AI. That is the bait and switch this function exists to prevent.
    """
    conversation = ctx.conversation
    if conversation.lead_type is LeadType.POST_SALES:
        return False

    slug = str(ctx.deps.knowledge_base.offer.get("course_slug", ""))
    if conversation.current_course:
        return conversation.current_course == slug

    # No course chosen yet: only the discovery branch, which exists to steer
    # toward the discounted course, may make the offer.
    return conversation.current_state is ConversationState.DISCOVERY


#: Engagement needed before the offer appears unprompted. Lower than the generic
#: callback threshold: this conversation is already about the course we are
#: selling, so the discount is the natural next thing rather than an interruption.
_OFFER_AFTER_EXCHANGES = 2


def is_closing_moment(text: str) -> bool:
    """A signal that the user is ready to hear the price and the code.

    Two kinds. Asking how to buy is obvious. Asking what it *costs* is the one
    that was being missed: the bot answered "Rs 4,999" and stopped, showing the
    full price while holding a 32% coupon - the worst possible reply to the
    question closest to a sale.
    """
    return intents.wants_to_enroll(text) or intents.asks_about_price(text)


def maybe_offer(ctx: TurnContext, *, force: bool = False) -> TurnResult | None:
    """The offer, if this is a good moment for it. Otherwise None.

    Fires immediately on a closing signal, and otherwise once the person has
    engaged for a couple of exchanges. Waiting for a generic question quota when
    someone has just asked the price is the clearest way to lose a sale.
    """
    conversation = ctx.conversation
    if not offer_is_live(ctx) or not _relevant(ctx):
        return None
    if conversation.current_state not in _SELLING_STATES:
        return None

    closing = force or is_closing_moment(ctx.text)
    if not closing and ctx.qna_count() < _OFFER_AFTER_EXCHANGES:
        return None

    course = ctx.deps.knowledge_base.upsell_course
    if course is None:  # pragma: no cover - offer names a course that must exist
        logger.warning("Offer configured for a course that is not in the catalogue")
        return None

    conversation.set_ctx(CTX_OFFER_SENT, True)
    # The offer already carries a "Talk to a Counselor" button, so it *is* the
    # callback nudge. Without consuming it here, the plain "shall a counselor
    # call you?" fired on the very next message - two escalation prompts back to
    # back, which reads as nagging.
    ctx.mark_nudged()
    logger.info(
        "Discount offer shown",
        extra={"state": str(conversation.current_state), "closing": closing},
    )

    result = TurnResult()
    result.add(copy.offer_message(ctx.deps.knowledge_base.offer, course.name))
    return result


def handle_offer_reply(ctx: TurnContext) -> TurnResult | None:
    """Reply to a tapped offer button, or None if it was not one.

    "Talk to a Counselor" is not handled here - it carries `MENU_COUNSELOR` and
    is routed by the machine's existing navigation, so escalation behaves
    identically wherever it is tapped from.
    """
    reply_id = ctx.inbound.reply_id
    if reply_id not in (copy.OFFER_DONE, copy.OFFER_QUESTION):
        return None

    conversation = ctx.conversation
    result = TurnResult()

    if reply_id == copy.OFFER_DONE:
        # No lead is created and nothing is claimed as paid: we cannot see the
        # payment, and telling a counselor someone enrolled when they only
        # tapped a button would put a false sale on the sheet.
        offer = ctx.deps.knowledge_base.offer
        result.add(
            OutboundMessage(
                text=copy.OFFER_ACCEPTED.format(
                    code=offer.get("coupon_code", ""),
                    url=offer.get("payment_url", ""),
                )
            )
        )
        return result

    conversation.current_state = (
        ConversationState.COURSE_QNA
        if conversation.current_course
        else ConversationState.DISCOVERY
    )
    result.add(OutboundMessage(text=copy.OFFER_QUESTION_PROMPT))
    return result


def wants_to_buy(text: str) -> bool:
    """An explicit buying signal, which earns the offer straight away."""
    return intents.wants_to_enroll(text)
