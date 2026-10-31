"""Run one case through the real answer pipeline, for a chosen prompt version.

This mirrors ``AnswerService.answer`` deliberately: the same TopicGuard, the same
BM25 retriever, the same prompt-assembly call. The *only* thing the eval varies
is which prompt module builds the system/user text. Retrieval and the guard are
held constant across versions on purpose - this isolates the prompt as the
single independent variable, so a score difference between two runs is caused by
the prompt and nothing else.

Model replies are cached on disk keyed by (provider, model, version, case id,
exact prompt text). A re-run with no prompt change is then free and
deterministic, which is what makes the regression gate cheap enough to run on
every change. Edit the prompt and the key changes, so the stale reply is never
silently reused.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, LLMError
from app.core.logging import get_logger
from app.services.knowledge.loader import KnowledgeBase, load_knowledge_base
from app.services.knowledge.retriever import KnowledgeRetriever, build_retriever
from app.services.llm.base import ChatTurn, LLMClient
from app.services.llm.guardrails import OFF_TOPIC_REPLY, TopicGuard
from evals.schema import Case

logger = get_logger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / ".cache"

#: Phrases that mark a provider error as a rate limit rather than a real fault.
_RATE_LIMIT_MARKERS = ("429", "rate_limit", "resource_exhausted", "quota")
#: "try again in 16.0s", "Please retry in 1.89s", "retry after 2s" - every
#: provider says how long to wait; this reads that number so the eval waits
#: exactly as long as it is told to instead of guessing.
_RETRY_AFTER_RE = re.compile(r"(?:try again in|retry (?:in|after))\s+([\d.]+)\s*s", re.IGNORECASE)


class PacedLLM:
    """Wraps a provider client and waits out rate limits instead of failing.

    Every LLM here is on a free tier (Gemini 5 req/min, Groq a few thousand
    tokens/min), so a straight run trips 429s. Rather than weaken the eval or
    edit the shipping clients, this decorator catches a rate-limit error, sleeps
    for as long as the provider asked, and retries. Real errors - a bad key, a
    missing model - are not rate limits and pass straight through. This is what
    lets the whole suite complete on a free key, just more slowly.
    """

    def __init__(self, inner: LLMClient, *, max_waits: int = 8, default_wait: float = 20.0) -> None:
        self._inner = inner
        self._max_waits = max_waits
        self._default_wait = default_wait

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(marker in text for marker in _RATE_LIMIT_MARKERS)

    def _wait_for(self, exc: Exception) -> float:
        match = _RETRY_AFTER_RE.search(str(exc))
        if match:
            # One second of slack so we are safely past the window, not on its edge.
            return math.ceil(float(match.group(1))) + 1.0
        return self._default_wait

    async def generate(self, **kwargs: Any) -> str:
        for attempt in range(self._max_waits + 1):
            try:
                return await self._inner.generate(**kwargs)
            except LLMError as exc:
                if not self._is_rate_limit(exc) or attempt == self._max_waits:
                    raise
                wait = self._wait_for(exc)
                logger.info("rate limited; waiting %.0fs then retrying", wait)
                await asyncio.sleep(wait)
        raise LLMError("exhausted rate-limit retries")  # pragma: no cover

    async def health_check(self) -> bool:
        return await self._inner.health_check()


# --------------------------------------------------------------------------- #
# Wiring - built once, reused for every case
# --------------------------------------------------------------------------- #
def build_llm(settings: Settings) -> LLMClient:
    """Construct the configured provider's client, exactly as the app does."""
    # Mirrors app/container.py so the eval talks to the same model the bot does.
    if settings.llm_provider == "groq":
        from app.services.llm.groq import GroqClient

        return GroqClient(settings)
    if settings.llm_provider == "gemini":
        from app.services.llm.gemini import GeminiClient

        return GeminiClient(settings)
    from app.services.llm.openai import OpenAIClient

    return OpenAIClient(settings)


def _judge_settings(settings: Settings) -> Settings:
    """A copy of settings pinned to temperature 0 for the judge.

    The judge is a measuring instrument; it should not wobble between runs. The
    answering model keeps its production temperature so we evaluate the bot as it
    actually behaves, but the grader is made as deterministic as the providers
    allow.
    """
    return settings.model_copy(
        update={
            "openai_temperature": 0.0,
            "gemini_temperature": 0.0,
            "groq_temperature": 0.0,
        }
    )


@dataclass(slots=True)
class Harness:
    """Everything needed to score cases, assembled once."""

    settings: Settings
    knowledge_base: KnowledgeBase
    retriever: KnowledgeRetriever
    guard: TopicGuard
    answer_llm: LLMClient
    judge_llm: LLMClient

    @classmethod
    def build(cls, settings: Settings | None = None) -> Harness:
        settings = settings or Settings()
        kb = load_knowledge_base(settings.knowledge_dir)
        retriever = build_retriever(
            kb,
            limit=settings.knowledge_max_snippets,
            max_chars=settings.knowledge_max_chars,
        )
        return cls(
            settings=settings,
            knowledge_base=kb,
            retriever=retriever,
            guard=TopicGuard(kb),
            # Paced so a free-tier 429 becomes a wait, not a failed case.
            answer_llm=PacedLLM(build_llm(settings)),
            judge_llm=PacedLLM(build_llm(_judge_settings(settings))),
        )

    @property
    def model_label(self) -> str:
        provider = self.settings.llm_provider
        model = getattr(self.settings, f"{provider}_model", "?")
        return f"{provider}:{model}"


# --------------------------------------------------------------------------- #
# Prompt assembly per version
# --------------------------------------------------------------------------- #
def _build_prompts(
    module: ModuleType, *, kb: KnowledgeBase, case: Case, snippets: list[Any]
) -> tuple[str, str]:
    """Assemble (system, user) prompts using whichever kwargs this version takes.

    The current prompt accepts upsell/known-profile/rules arguments the first one
    never had. Rather than branch on version, we build the full context the live
    code would and let each version take the subset of it that its signature
    declares - so adding a new prompt knob never means editing this runner.
    """
    course = kb.get_course(case.course)
    upsell = kb.upsell_course
    full_context: dict[str, Any] = {
        "company": kb.company_name,
        "state": case.state,
        "course_name": course.name if course else None,
        "nudge_callback": case.nudge_callback,
        "rules": kb.rules,
        "prompt_overrides": kb.prompt_overrides,
        "upsell_course": upsell.name if upsell else None,
        "known_profile": case.known_profile,
        # The persuasion guidance follows the course, not just the state - a
        # cohort-menu pick of the upsell course lands in COURSE_QNA but is still
        # the conversation the funnel exists to win. Copied from AnswerService.
        "selling_upsell": bool(upsell and case.course == upsell.slug),
    }

    accepted = inspect.signature(module.build_system_prompt).parameters
    kwargs = {k: v for k, v in full_context.items() if k in accepted}
    system_prompt = module.build_system_prompt(**kwargs)
    user_prompt = module.build_user_prompt(case.message, snippets)
    return system_prompt, user_prompt


# --------------------------------------------------------------------------- #
# Response cache
# --------------------------------------------------------------------------- #
def _cache_key(harness: Harness, version: str, case: Case, system: str, user: str) -> str:
    history = "|".join(f"{t.role}:{t.content}" for t in case.history)
    payload = "\n~~~\n".join(
        [harness.model_label, version, case.id, system, user, history]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


# --------------------------------------------------------------------------- #
# Output of running one case
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CaseOutput:
    """What the bot produced for one case, before any scoring."""

    text: str
    refused: bool
    sources: tuple[str, ...]
    system_prompt: str
    user_prompt: str
    #: Set when the model call failed (e.g. missing API key). Treated as neither
    #: pass nor fail - a broken instrument must not read as a passing system.
    error: str | None = None
    from_cache: bool = False


async def run_case(
    case: Case,
    *,
    harness: Harness,
    version: str,
    module: ModuleType,
    use_cache: bool = True,
) -> CaseOutput:
    """Reproduce ``AnswerService.answer`` for one case and the given prompt."""
    # 1. The guard, exactly as the service applies it. Discovery has just asked
    #    the user what they do, so a first-person answer is on topic there only.
    expecting_self = case.state.name == "DISCOVERY"
    if harness.guard.is_off_topic(case.message, expecting_self_description=expecting_self):
        return CaseOutput(
            text=OFF_TOPIC_REPLY,
            refused=True,
            sources=(),
            system_prompt="(refused before any model call)",
            user_prompt=case.message,
        )

    # 2. Retrieval, then prompt assembly for this version.
    snippets = harness.retriever.retrieve(
        case.message, course=case.course, audience=case.audience  # type: ignore[arg-type]
    )
    system_prompt, user_prompt = _build_prompts(
        module, kb=harness.knowledge_base, case=case, snippets=snippets
    )
    sources = tuple(s.id for s in snippets)

    # 3. The model call, through the disk cache.
    key = _cache_key(harness, version, case, system_prompt, user_prompt)
    if use_cache:
        cached = _cache_path(key)
        if cached.is_file():
            text = json.loads(cached.read_text(encoding="utf-8"))["text"]
            return CaseOutput(
                text=text,
                refused=False,
                sources=sources,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                from_cache=True,
            )

    try:
        text = await harness.answer_llm.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            history=list(case.history),
        )
    except (ConfigurationError, LLMError) as exc:
        return CaseOutput(
            text="",
            refused=False,
            sources=sources,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            error=f"{type(exc).__name__}: {exc}",
        )

    if use_cache:
        CACHE_DIR.mkdir(exist_ok=True)
        _cache_path(key).write_text(
            json.dumps({"text": text, "case": case.id, "version": version}),
            encoding="utf-8",
        )
    return CaseOutput(
        text=text,
        refused=False,
        sources=sources,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
