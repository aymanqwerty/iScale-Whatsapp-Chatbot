"""Pre-sales handlers: course selection and course / general Q&A."""

from __future__ import annotations

from app.bot import copy, intents
from app.bot.context import TurnContext
from app.bot.handlers.common import answer_question, answer_with_optional_nudge
from app.bot.handlers.discovery import remember_profile, start_discovery
from app.domain.enums import ConversationState, LeadType
from app.domain.messaging import OutboundMessage, TurnResult
from app.services.knowledge.models import Course


async def handle_course_group(ctx: TurnContext) -> TurnResult:
    """Cohort, advance, or undecided.

    A typed answer is honoured as readily as a tap - people reply "cohort" or
    "advance" to a button menu constantly, and bouncing them back to the same
    three buttons is the most irritating thing a bot can do.
    """
    result = TurnResult()
    conversation = ctx.conversation
    knowledge_base = ctx.deps.knowledge_base
    reply_id = ctx.inbound.reply_id or ""
    text = intents.normalize(ctx.text)

    if reply_id == copy.COURSE_UNSURE or intents.means_undecided(text):
        return start_discovery(ctx)

    group: str | None = None
    if reply_id.startswith(copy.GROUP_PREFIX):
        group = reply_id.removeprefix(copy.GROUP_PREFIX)
    elif "cohort" in text:
        group = "cohort"
    elif "advance" in text or "advanced" in text:
        group = "advance"

    if group in ("cohort", "advance"):
        conversation.current_state = ConversationState.COURSE_SELECTION
        conversation.lead_type = LeadType.PRE_SALES
        result.add(copy.course_group_submenu(knowledge_base, group))
        return result

    # Naming a course outright skips the group entirely.
    course = knowledge_base.match_course(ctx.text)
    if course is not None:
        return _select_course(ctx, course)

    # Anything else at this menu is a question, not a navigation attempt.
    if len(ctx.text.split()) >= 3:
        return await _answer_at_menu(ctx)

    result.add(OutboundMessage(text=copy.FALLBACK_UNRECOGNISED))
    result.add(copy.course_group_menu())
    return result


async def _answer_at_menu(ctx: TurnContext) -> TurnResult:
    """A question typed at a menu instead of a choice being tapped.

    Answered in GENERAL_QNA, not DISCOVERY. Discovery carries the AI For
    Everyone pitch, and someone asking "what are your working hours" wants the
    working hours - opening a sales pitch on them would be the bot ignoring a
    plain question. DISCOVERY is reached only by saying so, or by tapping
    "Not sure yet".
    """
    ctx.conversation.current_state = ConversationState.GENERAL_QNA
    ctx.conversation.lead_type = ctx.conversation.lead_type or LeadType.PRE_SALES
    result = TurnResult()
    result.add(OutboundMessage(text=await answer_question(ctx)))
    ctx.bump_qna_count()
    return result


def _select_course(ctx: TurnContext, course: Course) -> TurnResult:
    """Lock retrieval onto one course and confirm the choice."""
    conversation = ctx.conversation
    conversation.current_state = ConversationState.COURSE_QNA
    conversation.current_course = course.slug
    conversation.lead_type = LeadType.PRE_SALES
    result = TurnResult()
    result.add(
        OutboundMessage(
            text=copy.COURSE_SELECTED.format(
                course=course.name, summary=course.summary_line()
            )
        )
    )
    return result


async def handle_course_selection(ctx: TurnContext) -> TurnResult:
    """Pick a course from the list, by tap, name or number."""
    result = TurnResult()
    conversation = ctx.conversation
    knowledge_base = ctx.deps.knowledge_base

    reply_id = ctx.inbound.reply_id or ""

    if reply_id == copy.COURSE_UNSURE or intents.means_undecided(
        intents.normalize(ctx.text)
    ):
        return start_discovery(ctx)

    # "Other courses" opens the second-level list and stays in this state, so
    # the next tap is handled by exactly the same branch below.
    if reply_id == copy.COURSE_OTHERS:
        result.add(copy.other_courses_menu(knowledge_base))
        return result

    slug: str | None = None
    if reply_id.startswith(copy.COURSE_PREFIX):
        slug = reply_id.removeprefix(copy.COURSE_PREFIX)

    course = knowledge_base.get_course(slug) or knowledge_base.match_course(ctx.text)

    # A positional reply ("2") maps onto whichever submenu was last rendered.
    # Scoped to that group, not to every course we know about - "2" at the
    # cohort menu must never select the second course in the whole catalogue.
    if course is None:
        cleaned = intents.normalize(ctx.text)
        if cleaned.isdigit():
            index = int(cleaned) - 1
            courses = _last_group_courses(ctx)
            if 0 <= index < len(courses):
                course = courses[index]

    if course is not None:
        return _select_course(ctx, course)

    # Not a course name - most likely a question asked at the menu.
    if len(ctx.text.split()) >= 3:
        return await _answer_at_menu(ctx)

    result.add(OutboundMessage(text=copy.FALLBACK_UNRECOGNISED))
    result.add(copy.course_group_menu())
    conversation.current_state = ConversationState.COURSE_GROUP
    return result


def _last_group_courses(ctx: TurnContext) -> list[Course]:
    """Courses of the submenu the user is looking at.

    Inferred from the selected course's group when there is one, falling back to
    cohort - the branch this funnel pushes hardest, and the menu a user is most
    likely to be sitting on.
    """
    knowledge_base = ctx.deps.knowledge_base
    current = knowledge_base.get_course(ctx.conversation.current_course)
    group = current.group if current and current.group else "cohort"
    return knowledge_base.courses_in_group(group)


async def handle_course_qna(ctx: TurnContext) -> TurnResult:
    """Unlimited questions about the selected course."""
    # Switching course mid-conversation is common ("what about data science?").
    course = ctx.deps.knowledge_base.match_course(ctx.text)
    if course is not None and course.slug != ctx.conversation.current_course:
        ctx.conversation.current_course = course.slug

    # People volunteer their job here as readily as in discovery - "how does it
    # help me as a doctor?" is the same question, asked from the menu instead.
    # Capturing it means the answer is tailored and the lead reaches the
    # counselor with the context already attached.
    remember_profile(ctx)

    return await answer_with_optional_nudge(ctx)


async def handle_general_qna(ctx: TurnContext) -> TurnResult:
    """Career advice, comparisons, roadmaps - anything not course-scoped."""
    course = ctx.deps.knowledge_base.match_course(ctx.text)
    if course is not None:
        # The user has settled on a course; narrow the retrieval scope to it.
        ctx.conversation.current_course = course.slug
        ctx.conversation.current_state = ConversationState.COURSE_QNA

    return await answer_with_optional_nudge(ctx)
