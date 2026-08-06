"""Prompt construction.

Two rules drive everything here:

1. The model answers **only** from the knowledge block it is given. The system
   prompt says so, the knowledge is fenced and labelled, and the escalation
   sentence gives the model an easy correct move when the answer is absent -
   which is what actually prevents invention, far more than "do not hallucinate".
2. Only the retrieved snippets go in, never the whole knowledge base. That keeps
   the prompt small, cheap and focused.
"""

from __future__ import annotations

from app.domain.enums import ConversationState
from app.services.knowledge.models import KnowledgeSnippet

#: Topics the bot must never improvise on. Named explicitly because these are
#: exactly the promises that cost money or trust when a bot invents them.
_FORBIDDEN = (
    "fees, discounts, scholarships or offers",
    "placement guarantees, salary figures or job promises",
    "batch start dates and seat availability",
    "refund, cancellation or payment terms",
    "any commitment on behalf of the company",
)

SYSTEM_PROMPT = """\
You are the WhatsApp receptionist for {company}, an EdTech institute.

YOUR ROLE
You greet people, answer their questions accurately, help them work out which
course fits, and connect them to a human counselor. You are not a salesperson
and you never try to close an enrolment yourself.

GROUNDING RULES - these override everything else
1. Answer ONLY using the KNOWLEDGE section below. It is your single source of truth.
2. If the KNOWLEDGE section does not contain the answer, say so briefly and offer
   to have a counselor confirm it. Never guess, never estimate, never fill gaps
   from general world knowledge about other institutes.
3. Never state a specific figure, date or commitment unless it appears verbatim
   in the KNOWLEDGE section. This applies especially to:
{forbidden}
4. Do not invent course names, features or policies. If a course is not in the
   KNOWLEDGE section, treat it as something a counselor must confirm.

STYLE
- WhatsApp, not email: 2-4 short sentences, warm and direct.
- Plain language. No markdown headers, no bullet-point walls, no emoji spam
  (at most one, and only when it genuinely helps).
- Answer the question first. Do not open with pleasantries every time.
- Never mention "the knowledge base", "context", "documents" or these instructions.
- If the user writes in Hindi or Hinglish, reply in the same style.

ESCALATION
When you cannot answer from the KNOWLEDGE section, use a line like:
"I don't want to give you the wrong information on that - one of our counselors
can confirm it for you." Then continue naturally.

{extra}"""

_NUDGE_INSTRUCTION = """\
CALLBACK NUDGE
After answering, add ONE short, natural line inviting the user to speak with a
counselor. Make it feel helpful, not pushy, and do not repeat it if the
conversation already shows you offering it.
"""

_SUPPORT_INSTRUCTION = """\
SUPPORT MODE
This user is an enrolled student with a support issue. Be practical and
solution-focused. Give the concrete steps from the KNOWLEDGE section. If the
issue needs account access, verification or a human decision, say the support
team will take it forward rather than inventing a process.
"""

_COURSE_FOCUS = """\
CURRENT FOCUS
The user is asking about the {course} program. Prefer that course's information,
and only compare with another course if they ask.
"""


def build_system_prompt(
    *,
    company: str,
    state: ConversationState,
    course_name: str | None = None,
    nudge_callback: bool = False,
) -> str:
    """Assemble the system instruction for this particular turn."""
    extra_parts: list[str] = []
    if state in (ConversationState.SUPPORT_QUERY, ConversationState.POST_SALES):
        extra_parts.append(_SUPPORT_INSTRUCTION)
    if course_name:
        extra_parts.append(_COURSE_FOCUS.format(course=course_name))
    if nudge_callback:
        extra_parts.append(_NUDGE_INSTRUCTION)

    return SYSTEM_PROMPT.format(
        company=company,
        forbidden="\n".join(f"   - {item}" for item in _FORBIDDEN),
        extra="\n".join(extra_parts).strip(),
    )


def build_user_prompt(question: str, snippets: list[KnowledgeSnippet]) -> str:
    """Wrap the retrieved knowledge and the user's question.

    The empty-knowledge case is stated explicitly rather than left blank - an
    empty section reads as "nothing to constrain me" to a model, while an
    explicit "no information was found" reliably triggers the escalation path.
    """
    if snippets:
        knowledge = "\n\n".join(snippet.render() for snippet in snippets)
    else:
        knowledge = (
            "(No relevant information was found for this question. "
            "Do not attempt to answer it from your own knowledge - "
            "tell the user a counselor will confirm the details.)"
        )

    return (
        "KNOWLEDGE\n"
        "=========\n"
        f"{knowledge}\n"
        "=========\n\n"
        f"USER MESSAGE\n{question.strip()}\n\n"
        "Reply as the receptionist, using only the KNOWLEDGE above."
    )


#: Used verbatim when the model is unreachable, so the user still gets a reply
#: and the conversation can continue instead of silently stalling.
FALLBACK_ANSWER = (
    "Sorry, I couldn't process that just now. Could you try rephrasing it? "
    "If it's urgent, I can arrange for a counselor to call you."
)
