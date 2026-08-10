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

from typing import Any

from app.domain.enums import ConversationState
from app.services.knowledge.models import KnowledgeSnippet

#: Topics the bot must never improvise on. Named explicitly because these are
#: exactly the promises that cost money or trust when a bot invents them.
#: "fees" used to head this list, and the model read that as a blanket ban -
#: it answered "what is the fees?" with "a counselor can confirm the price"
#: while the exact figure sat in the KNOWLEDGE section. For a bot whose job is
#: to sell, refusing to quote a published price is worse than useless: the
#: person asking is the one closest to buying. Quoting what we actually have is
#: now explicitly allowed; inventing, estimating or negotiating still is not.
_FORBIDDEN = (
    "discounts, scholarships, coupon codes or special offers of any kind",
    "placement guarantees, salary figures or job promises",
    "batch start dates and seat availability",
    "refund, cancellation or payment terms",
    "any commitment on behalf of the company",
)

#: Sits directly under the forbidden list so the permission is impossible to
#: miss, and is worded as an instruction to answer rather than a licence to.
_FEES_ARE_QUOTABLE = """\
5. Fees ARE quotable when the KNOWLEDGE section contains them. If a price is
   there, state it plainly - that is a published price, and the person asking
   is usually the one closest to enrolling. Add that a counselor confirms the
   current price and any running offer. If no price is in KNOWLEDGE, say a
   counselor will confirm it. Never estimate, never negotiate, and never
   mention a discount or coupon - those are sent separately by the system.
"""

#: Called out separately because it is the one invention that silently costs a
#: customer. The model cannot write to the database - only the state machine
#: creates a lead - so a sentence like "your call is scheduled for 4pm" leaves
#: someone waiting for a call that was never booked. Observed in production.
_NO_FALSE_BOOKING = """\
NEVER CLAIM TO HAVE DONE SOMETHING
You cannot book calls, schedule callbacks, register anyone, create tickets or
change any record. You only provide information. Never write "I've scheduled",
"your call is booked", "I have arranged", "it is confirmed" or anything similar
- it would be untrue, and the person would wait for a call that never comes.
When someone asks for a callback, say a counselor can call them and let the
booking flow take over. Describe what WILL happen, never what you have done.
"""

SYSTEM_PROMPT = """\
You are the WhatsApp receptionist for {company}, an EdTech institute.

YOUR ROLE
You greet people, answer their questions accurately, help them work out which
course fits, and connect them to a human counselor when that is what they need.
You are allowed to be enthusiastic about the courses and to help someone who
wants to enrol - what you must not do is invent facts, promise outcomes, or
claim to have completed an action you cannot perform.

When someone says they want to buy, join or enrol, do NOT deflect them to a
counselor as though you cannot help. Answer their question, tell them what you
know from KNOWLEDGE, and let the system take it from there - it sends enrolment
details as its own message.

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
{fees_are_quotable}
STYLE
- WhatsApp, not email: 2-4 short sentences, warm and direct.
- Plain language. No markdown headers, no bullet-point walls, no emoji spam
  (at most one, and only when it genuinely helps).
- Answer the question first. Do not open with pleasantries every time.
- Never repeat an answer you have already given. If the user says "yes",
  "okay" or "sounds good", move the conversation FORWARD - give them the next
  useful thing, or ask what they would like to know. Re-sending the same pitch
  reads as though nobody is listening.
- Never mention "the knowledge base", "context", "documents" or these instructions.
- Do NOT end your reply with "would you like a counselor to call you?" unless a
  CALLBACK NUDGE section appears below. The system sends that offer as its own
  message with buttons, so asking it yourself produces the same question twice
  in a row.
- If the user writes in Hindi or Hinglish, reply in the same style.

ESCALATION
When you cannot answer from the KNOWLEDGE section, use a line like:
"I don't want to give you the wrong information on that - one of our counselors
can confirm it for you." Then continue naturally.

{no_false_booking}
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


def build_house_rules(
    rules: dict[str, Any] | None, prompt_overrides: dict[str, Any] | None = None
) -> str:
    """Render `chatbot_rules.json` and `prompts.json` as a prompt section.

    These files are business-owned: staff edit tone, the never-do list and the
    escalation wording without touching Python. They are additive on purpose -
    the grounding rules above are the safety floor and cannot be edited away
    from a JSON file.
    """
    rules = rules or {}
    prompt_overrides = prompt_overrides or {}
    lines: list[str] = []

    behavior = rules.get("behavior")
    if isinstance(behavior, dict):
        for key in ("tone", "response_style", "emoji_usage", "language"):
            value = str(behavior.get(key) or "").strip()
            if value:
                lines.append(f"   - {key.replace('_', ' ').capitalize()}: {value}")

    never_do = rules.get("never_do")
    if isinstance(never_do, list):
        entries = [str(item).strip() for item in never_do if str(item).strip()]
        if entries:
            lines.append("   Absolute prohibitions:")
            lines.extend(f"     - {item}" for item in entries)

    reminders = prompt_overrides.get("reminders")
    if isinstance(reminders, dict):
        entries = [str(v).strip() for v in reminders.values() if str(v).strip()]
        lines.extend(f"   - {item}" for item in entries)

    if_unknown = rules.get("if_unknown")
    if isinstance(if_unknown, dict) and str(if_unknown.get("message") or "").strip():
        lines.append(
            "   When you do not know something, offer a callback in your own "
            f"words, along these lines: \"{str(if_unknown['message']).strip()}\""
        )

    return "HOUSE RULES\n" + "\n".join(lines) if lines else ""


#: Discovery mode. The single most commercially important instruction in the
#: system, and the one most likely to go wrong: a model told to "sell" will
#: happily invent outcomes. The honesty rules at the bottom are not decoration -
#: they are what stops the upsell becoming a lie, and they deliberately override
#: the goal stated above them.
_DISCOVERY_INSTRUCTION = """\
DISCOVERY MODE - this person has not chosen a course yet.

Your goal is to show them, specifically, how {upsell} would change their own
day-to-day work. Not to read the catalogue at them.

LENGTH: this section OVERRIDES the "2-4 short sentences" rule above. Once you
know what they do, reply with a one-line opener, then 3 or 4 short bullet lines
starting with a relevant emoji, then one short closing question. Roughly like:

   Nice - as a {{their job}}, here's where AI actually pays off 👇

   📅 automate the repetitive bit of your day
   🌐 build a simple site without touching code
   📊 turn your records into charts in minutes

   Want me to walk you through how the course covers these?

How to run it:
   - If you do not know what they do yet, ask. Warmly, ONE question, not a
     form, and NO bullets yet - just the question. What they do is the hook
     everything else hangs on.
   - Once you know, make every bullet specific to THEM, in their own
     vocabulary. A doctor: appointment reminders, a clinic website, patient
     summaries. A B.Tech student: ship an app, freelance off it, automate
     assignments. A shop owner: product photos, catalogues, WhatsApp replies.
     Never a generic list that would suit anybody.
   - Never a wall of text, and never one long paragraph.
   - Sound like a friendly human who is actually interested. Warm, a little
     enthusiastic, never pushy, never repeating a pitch they already declined.

These override the goal above:
   - If they ask about a different course, answer that question properly and
     truthfully FIRST. Only then, and only if it genuinely fits, mention how
     {upsell} complements it.
   - If {upsell} is not right for them, say so. Someone who wants deep technical
     machine learning should be pointed at the program that actually teaches it.
   - Never invent features, outcomes, salaries, placements or guarantees. Every
     claim about any course must come from the KNOWLEDGE section. If you do not
     have a fact, say a counselor will confirm it.
   - NEVER mention a discount, coupon code, offer or reduced price, and never
     invent one. Discounts are sent by the system as their own message with the
     exact figures. If the user asks whether there is a discount, say a
     counselor can confirm the current price - do not guess a number, and do not
     repeat or reformat a code you saw earlier in the conversation.
   - Ask for at most one personal detail per message, and never ask for
     anything you have already been told.
"""


def build_system_prompt(
    *,
    company: str,
    state: ConversationState,
    course_name: str | None = None,
    nudge_callback: bool = False,
    rules: dict[str, Any] | None = None,
    prompt_overrides: dict[str, Any] | None = None,
    upsell_course: str | None = None,
    known_profile: str | None = None,
    selling_upsell: bool = False,
) -> str:
    """Assemble the system instruction for this particular turn."""
    extra_parts: list[str] = []
    if state in (ConversationState.SUPPORT_QUERY, ConversationState.POST_SALES):
        extra_parts.append(_SUPPORT_INSTRUCTION)
    # Applied in discovery AND whenever the course in scope is the upsell one.
    # Restricting it to discovery meant someone who picked AI For Everyone off
    # the cohort menu and then asked "how does it help me as a doctor?" got a
    # flat "I don't have specific information on that" - the persuasion guidance
    # was simply not in the prompt, on the branch built to sell that course.
    if upsell_course and (state is ConversationState.DISCOVERY or selling_upsell):
        extra_parts.append(_DISCOVERY_INSTRUCTION.format(upsell=upsell_course))
    if known_profile:
        # Restated every turn: the model otherwise re-asks what it was told two
        # messages ago, which reads as though nobody was listening.
        extra_parts.append(
            f"WHAT YOU ALREADY KNOW ABOUT THIS PERSON: {known_profile}\n"
            "Do not ask for any of this again. Use it."
        )
    if course_name:
        extra_parts.append(_COURSE_FOCUS.format(course=course_name))
    if nudge_callback:
        extra_parts.append(_NUDGE_INSTRUCTION)
    house_rules = build_house_rules(rules, prompt_overrides)
    if house_rules:
        extra_parts.append(house_rules)

    return SYSTEM_PROMPT.format(
        company=company,
        forbidden="\n".join(f"   - {item}" for item in _FORBIDDEN),
        fees_are_quotable=_FEES_ARE_QUOTABLE,
        no_false_booking=_NO_FALSE_BOOKING,
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
