"""Per-turn context passed to every state handler."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.db.models.conversation import Conversation
from app.db.models.user import User
from app.domain.messaging import InboundMessage
from app.repositories.lead_repository import LeadRepository
from app.services.knowledge.loader import KnowledgeBase
from app.services.lead_service import LeadService
from app.services.llm.answer_service import AnswerService
from app.services.llm.base import ChatTurn
from app.services.scheduling.callback_time import CallbackTimeValidator

# Context keys, named once so a typo cannot silently create a second key.
CTX_QNA_COUNT = "qna_count"
CTX_NUDGED = "callback_nudged"
CTX_PENDING_NAME = "pending_name"
CTX_PENDING_TIME = "pending_time"          # ISO-8601, business timezone
CTX_PENDING_TIME_RAW = "pending_time_raw"
CTX_SUPPORT_TOPIC = "support_topic"
CTX_RETURN_STATE = "return_state"          # where to resume if a callback is declined
CTX_NAME_ATTEMPTS = "name_attempts"
#: The date the user named on a rejected attempt, so a follow-up that gives
#: only a time ("4:30 pm then") lands on the day they actually asked for.
CTX_PENDING_DATE = "pending_date"
#: Set while moving an existing booking. When present at the end of the flow the
#: lead is UPDATED rather than a second one created, so the counselor never sees
#: the same person twice with two different times.
CTX_RESCHEDULE_LEAD_ID = "reschedule_lead_id"

#: What the person has told us about themselves ("final year B.Tech student").
#: Fed back into every prompt so the bot stops re-asking, and written to the
#: lead so a counselor opens the call already knowing who they are calling.
CTX_PROFILE = "profile"

#: Post-sales capture slots. A paid student's callback is only written once
#: name, email and enrolled course are all present - these hold them until then.
#: Set once the discount has been shown. A coupon repeated every few messages
#: stops reading as a favour and starts reading as pressure.
CTX_OFFER_SENT = "offer_sent"

CTX_EMAIL = "email"
CTX_ENROLLED_COURSE = "enrolled_course"
CTX_CONTACT_PHONE = "contact_phone"


@dataclass(frozen=True, slots=True)
class BotDependencies:
    """Everything the state machine needs, injected once per request.

    Handlers receive collaborators rather than importing them, which is what
    makes each handler testable in isolation with fakes.
    """

    settings: Settings
    knowledge_base: KnowledgeBase
    answer_service: AnswerService
    callback_validator: CallbackTimeValidator
    lead_service: LeadService
    #: The lead repository, for the reschedule flow. Handlers need to look up a
    #: booking made in an *earlier* conversation, which `lead_service` (scoped
    #: to creating one) cannot answer.
    lead_repository: LeadRepository


@dataclass(slots=True)
class TurnContext:
    """One inbound message plus the state it is being applied to."""

    inbound: InboundMessage
    user: User
    conversation: Conversation
    deps: BotDependencies
    #: Recent transcript, loaded once per turn and handed to the LLM so follow-up
    #: questions ("and the fees for that one?") resolve correctly.
    history: tuple[ChatTurn, ...] = ()

    @property
    def text(self) -> str:
        return self.inbound.text

    @property
    def company(self) -> str:
        return self.deps.knowledge_base.company_name

    def qna_count(self) -> int:
        return int(self.conversation.get_ctx(CTX_QNA_COUNT, 0))

    def bump_qna_count(self) -> int:
        count = self.qna_count() + 1
        self.conversation.set_ctx(CTX_QNA_COUNT, count)
        return count

    def should_nudge_callback(self) -> bool:
        """True once the user has asked enough questions to warrant an offer.

        Only ever fires once per conversation - a bot that asks "shall I have
        someone call you?" after every answer is the thing users complain about.
        """
        if self.conversation.get_ctx(CTX_NUDGED, False):
            return False
        return self.qna_count() >= self.deps.settings.qna_nudge_threshold

    def mark_nudged(self) -> None:
        self.conversation.set_ctx(CTX_NUDGED, True)
