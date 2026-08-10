"""The discovery branch: work out what the person does, then pitch to that.

Reached from "Not sure yet" and from a free-course student who cannot be
offered support. Both are people with intent and no chosen course, which is
exactly who AI For Everyone is for - cheapest paid course, no prerequisites, and
useful in any job.

The persuasion itself is the model's work, steered by `_DISCOVERY_INSTRUCTION`
in `prompts.py`. What lives here is everything that must NOT be left to a model:
which course the answer is grounded in, what we remember about the person, and
when a callback is offered.
"""

from __future__ import annotations

import re

from app.bot import copy
from app.bot.context import CTX_PROFILE, TurnContext
from app.bot.handlers.common import answer_question, offer_callback
from app.core.logging import get_logger
from app.domain.enums import ConversationState, LeadType
from app.domain.messaging import OutboundMessage, TurnResult

logger = get_logger(__name__)

#: How the person describes themselves. Deliberately broad and deliberately
#: not an LLM call: this runs on every discovery turn, and one extra round trip
#: per message would double the latency of the branch the funnel depends on.
_PROFILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:i\s*am|i'm|im|mai|main|me)\b[^.!?\n]{0,60}", re.IGNORECASE
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:work|study|teach|run|own|freelance)\b[^.!?\n]{0,60}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:doing|pursuing|studying)\s+[^.!?\n]{0,60}", re.IGNORECASE
    ),
)

#: Words that mean the message is about a course, not about the person. Without
#: this, "I am interested in the data science course" is stored as a profession.
_NOT_A_PROFILE = (
    "interested", "looking for", "asking", "confused", "not sure", "unsure",
    "fine", "ok", "okay", "good", "well", "here", "ready", "back",
)

_MAX_PROFILE_CHARS = 160


def start_discovery(ctx: TurnContext, *, opener: str | None = None) -> TurnResult:
    """Enter discovery and ask the opening question.

    `current_course` is cleared: whatever they were reading about before, they
    have just said they are undecided, and leaving it set would scope every
    subsequent answer to a course they did not choose.
    """
    conversation = ctx.conversation
    conversation.current_state = ConversationState.DISCOVERY
    conversation.lead_type = LeadType.PRE_SALES
    conversation.current_course = None

    result = TurnResult()
    result.add(OutboundMessage(text=opener or copy.UNSURE_COURSE))
    return result


async def handle_discovery(ctx: TurnContext) -> TurnResult:
    """One discovery turn: remember who they are, answer, and steer."""
    conversation = ctx.conversation

    remember_profile(ctx)

    # Ground the answer in whichever course they named; otherwise in the upsell,
    # so the model is arguing from real AI For Everyone facts rather than from
    # whatever the retriever happened to surface.
    knowledge_base = ctx.deps.knowledge_base
    named = knowledge_base.match_course(ctx.text)
    upsell = knowledge_base.upsell_course
    if named is not None:
        conversation.current_course = named.slug
        scope = named.slug
    else:
        scope = upsell.slug if upsell else None

    answer = await answer_question(ctx, course_slug=scope)

    result = TurnResult()
    result.add(OutboundMessage(text=answer))

    # Offer the call once they have actually engaged - a callback offered on the
    # first reply, before we know anything, converts badly and reads as a
    # brush-off. `should_nudge` fires once per conversation.
    if ctx.bump_qna_count() >= ctx.deps.settings.qna_nudge_threshold and ctx.should_nudge():
        ctx.mark_nudged()
        result.replies.extend(
            offer_callback(ctx, resume_state=ConversationState.DISCOVERY).replies
        )
    return result


def remember_profile(ctx: TurnContext) -> str | None:
    """Store what the user just said about themselves, if anything.

    Appends rather than replaces, up to a cap: people reveal themselves in
    pieces ("I'm a student" ... "final year, mechanical"), and the second
    message is usually the one that makes the pitch specific.
    """
    text = (ctx.text or "").strip()
    if not text:
        return None

    lowered = text.lower()
    if any(token in lowered for token in _NOT_A_PROFILE):
        return None

    for pattern in _PROFILE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        fragment = " ".join(match.group(0).split()).strip(" .,!?")
        if len(fragment) < 6:
            continue

        existing = str(ctx.conversation.get_ctx(CTX_PROFILE, "") or "")
        if fragment.lower() in existing.lower():
            return existing
        combined = f"{existing}; {fragment}" if existing else fragment
        combined = combined[:_MAX_PROFILE_CHARS]
        ctx.conversation.set_ctx(CTX_PROFILE, combined)
        logger.info("Captured profile detail", extra={"chars": len(combined)})
        return combined

    return None


def known_profile(ctx: TurnContext) -> str | None:
    value = str(ctx.conversation.get_ctx(CTX_PROFILE, "") or "").strip()
    return value or None
