"""Answer service - retrieval, prompting and the LLM call in one place.

The state machine asks a single question ("answer this, for this user, in this
state") and gets back text. It knows nothing about retrieval or prompts, and the
LLM knows nothing about the conversation flow.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.domain.enums import ConversationState
from app.services.knowledge.loader import KnowledgeBase
from app.services.knowledge.models import Audience
from app.services.knowledge.retriever import KnowledgeRetriever
from app.services.llm.base import ChatTurn, LLMClient
from app.services.llm.guardrails import OFF_TOPIC_REPLY, TopicGuard
from app.services.llm.prompts import (
    FALLBACK_ANSWER,
    build_system_prompt,
    build_user_prompt,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AnswerRequest:
    """Everything needed to answer one user question."""

    question: str
    state: ConversationState
    course_slug: str | None = None
    audience: Audience = "all"
    history: tuple[ChatTurn, ...] = ()
    nudge_callback: bool = False


@dataclass(frozen=True, slots=True)
class AnswerResult:
    text: str
    #: Snippet ids used, for logging and for debugging bad answers.
    sources: tuple[str, ...] = ()
    #: True when the LLM failed and the canned fallback was returned instead.
    degraded: bool = False
    #: True when the topic guard refused before any model call.
    refused: bool = False


class AnswerService:
    def __init__(
        self,
        *,
        llm: LLMClient,
        retriever: KnowledgeRetriever,
        knowledge_base: KnowledgeBase,
        guard: TopicGuard | None = None,
    ) -> None:
        self._llm = llm
        self._retriever = retriever
        self._kb = knowledge_base
        self._guard = guard or TopicGuard(knowledge_base)

    async def answer(self, request: AnswerRequest) -> AnswerResult:
        # The gate lives here rather than in the handlers because this is the
        # only route to the model - a new handler cannot forget to apply it.
        if self._guard.is_off_topic(request.question):
            logger.info(
                "Refused an off-topic message before calling the model",
                extra={
                    "state": str(request.state),
                    "injection": self._guard.is_injection(request.question),
                    "preview": request.question[:80],
                },
            )
            return AnswerResult(text=OFF_TOPIC_REPLY, refused=True)

        course = self._kb.get_course(request.course_slug)

        snippets = self._retriever.retrieve(
            request.question,
            course=request.course_slug,
            audience=request.audience,
        )

        system_prompt = build_system_prompt(
            company=self._kb.company_name,
            state=request.state,
            course_name=course.name if course else None,
            nudge_callback=request.nudge_callback,
            rules=self._kb.rules,
            prompt_overrides=self._kb.prompt_overrides,
        )
        user_prompt = build_user_prompt(request.question, snippets)

        try:
            text = await self._llm.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                history=list(request.history),
            )
        except Exception:
            # The LLM is a degradable dependency: nothing it raises may break the
            # turn. Deliberately broad - as well as timeouts and outages
            # (`LLMError`), this catches a missing API key (`ConfigurationError`)
            # and any surprise from the vendor SDK. Without it the exception
            # escapes to the orchestrator, the turn is rolled back, and the user
            # gets an error message instead of an answer plus a way forward.
            # Scope is tight: only the network call is inside the block.
            logger.exception(
                "LLM call failed - falling back to the canned answer",
                extra={"state": str(request.state), "course": request.course_slug},
            )
            return AnswerResult(text=FALLBACK_ANSWER, degraded=True)

        logger.info(
            "Answered from knowledge base",
            extra={
                "state": str(request.state),
                "course": request.course_slug,
                "sources": [s.id for s in snippets],
            },
        )
        return AnswerResult(text=text, sources=tuple(s.id for s in snippets))
