"""LLM-as-judge for the semantic checks a regex cannot make.

Kept deliberately small and strict. The judge sees only the user message, the
bot's reply and a single yes/no rubric, and must answer as JSON. It is never
asked to rate quality on a scale or to be creative - a binary "does this reply
satisfy this specific rubric" is far more stable across runs than a 1-10 score,
and it is all the eval needs.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from evals.pipeline import CACHE_DIR

if TYPE_CHECKING:
    from app.services.llm.base import LLMClient

_JUDGE_SYSTEM = """\
You are a strict evaluator of a WhatsApp receptionist bot for an EdTech company.
You are given the user's message, the bot's reply, and ONE yes/no rubric.
Decide only whether the reply satisfies that rubric. Do not judge anything else,
do not rewrite the reply, do not be lenient to be kind.

Respond with ONLY a JSON object on a single line, no prose, no code fence:
{"pass": true or false, "reason": "<=15 words"}"""

_JUDGE_USER = """\
USER MESSAGE:
{message}

BOT REPLY:
{reply}

RUBRIC (the reply passes only if this is true):
{rubric}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_verdict(raw: str) -> tuple[bool, str]:
    """Pull a {pass, reason} verdict out of the judge's text.

    Tolerant on purpose: models wrap JSON in stray prose or a code fence often
    enough that a bare ``json.loads`` would throw on an otherwise fine verdict.
    A reply we genuinely cannot parse fails closed, and says so, rather than
    being counted as a pass.
    """
    match = _JSON_RE.search(raw)
    if not match:
        return False, f"judge returned no JSON: {raw[:80]!r}"
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return False, f"judge JSON was malformed: {raw[:80]!r}"
    passed = bool(data.get("pass"))
    reason = str(data.get("reason") or "").strip() or "(no reason given)"
    return passed, reason


def _verdict_cache_path(model_label: str, rubric: str, reply: str) -> "Path":
    key = hashlib.sha256(f"{model_label}\n~~~\n{rubric}\n~~~\n{reply}".encode("utf-8")).hexdigest()
    return CACHE_DIR / f"judge_{key}.json"


async def judge_reply(
    judge_llm: "LLMClient",
    *,
    message: str,
    reply: str,
    rubric: str,
    model_label: str = "",
    use_cache: bool = True,
) -> tuple[bool, str]:
    """Return (passed, reason) for one rubric against one reply.

    The verdict is cached on disk keyed by (model, rubric, reply). A judged reply
    that has not changed gets the same verdict without a model call, so a re-run
    is both free and identical - which is what lets the judge sit in a regression
    gate without turning it flaky.
    """
    if not reply.strip():
        return False, "empty reply (nothing to judge)"

    cache_path = _verdict_cache_path(model_label, rubric, reply)
    if use_cache and cache_path.is_file():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return bool(data["pass"]), str(data.get("reason", ""))

    verdict = await judge_llm.generate(
        system_prompt=_JUDGE_SYSTEM,
        user_prompt=_JUDGE_USER.format(message=message, reply=reply, rubric=rubric),
        history=[],
    )
    passed, reason = _parse_verdict(verdict)
    if use_cache:
        CACHE_DIR.mkdir(exist_ok=True)
        cache_path.write_text(json.dumps({"pass": passed, "reason": reason}), encoding="utf-8")
    return passed, reason
