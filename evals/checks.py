"""Checks that score one reply.

Two tiers, and the split is the same judgement the bot itself is built on:

* **Deterministic checks** (contains / regex / refusal / length) decide anything
  a string operation can decide for sure - whether a published price appears,
  whether a false "your call is booked" slipped through, whether the guard
  refused. They are free, instant and never flaky, so they carry as much of the
  eval as possible.
* **The LLM judge** is used only for the genuinely semantic questions a regex
  cannot answer: "is this discovery pitch specific to a shop owner?", "is the
  reply in Hinglish?". It costs a model call and can disagree with itself, so it
  is the exception, not the rule.

That is the same line the system draws between deterministic code and the model,
applied to the eval - which is the honest way to keep an eval trustworthy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from evals.judge import judge_reply

if TYPE_CHECKING:
    from app.services.llm.base import LLMClient
    from evals.pipeline import CaseOutput
    from evals.schema import Case

_SENTENCE_RE = re.compile(r"[.!?]+(?:\s|$)")


@dataclass(frozen=True, slots=True)
class CheckResult:
    passed: bool
    #: The check type, for the report ("contains_any", "judge", ...).
    kind: str
    #: Short human reason, shown only on failure.
    detail: str


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _lower(text: str) -> str:
    return text.lower()


# --------------------------------------------------------------------------- #
# Deterministic checks. Each returns (passed, detail).
# --------------------------------------------------------------------------- #
def _check_contains_any(text: str, spec: dict[str, Any]) -> tuple[bool, str]:
    needles = _as_list(spec.get("any") or spec.get("value"))
    hay = _lower(text)
    hit = next((n for n in needles if n.lower() in hay), None)
    if hit is not None:
        return True, f"found {hit!r}"
    return False, f"none of {needles} present"


def _check_contains_all(text: str, spec: dict[str, Any]) -> tuple[bool, str]:
    needles = _as_list(spec.get("all"))
    hay = _lower(text)
    missing = [n for n in needles if n.lower() not in hay]
    if missing:
        return False, f"missing {missing}"
    return True, "all present"


def _check_not_contains(text: str, spec: dict[str, Any]) -> tuple[bool, str]:
    needles = _as_list(spec.get("none") or spec.get("value"))
    hay = _lower(text)
    hit = next((n for n in needles if n.lower() in hay), None)
    if hit is not None:
        return False, f"contained forbidden {hit!r}"
    return True, "none of the forbidden phrases present"


def _check_regex(text: str, spec: dict[str, Any]) -> tuple[bool, str]:
    pattern = str(spec.get("pattern", ""))
    if re.search(pattern, text, re.IGNORECASE):
        return True, f"matched /{pattern}/"
    return False, f"did not match /{pattern}/"


def _check_not_regex(text: str, spec: dict[str, Any]) -> tuple[bool, str]:
    pattern = str(spec.get("pattern", ""))
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return False, f"matched forbidden /{pattern}/ at {match.group(0)!r}"
    return True, f"no match for /{pattern}/"


def _check_max_words(text: str, spec: dict[str, Any]) -> tuple[bool, str]:
    limit = int(spec.get("max", 0))
    count = len(text.split())
    if count <= limit:
        return True, f"{count} words <= {limit}"
    return False, f"{count} words exceeds {limit} (WhatsApp replies stay short)"


def _check_max_sentences(text: str, spec: dict[str, Any]) -> tuple[bool, str]:
    limit = int(spec.get("max", 0))
    count = len([s for s in _SENTENCE_RE.split(text) if s.strip()])
    if count <= limit:
        return True, f"{count} sentences <= {limit}"
    return False, f"{count} sentences exceeds {limit}"


_DETERMINISTIC = {
    "contains_any": _check_contains_any,
    "contains_all": _check_contains_all,
    "not_contains": _check_not_contains,
    "regex": _check_regex,
    "not_regex": _check_not_regex,
    "max_words": _check_max_words,
    "max_sentences": _check_max_sentences,
}


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
async def run_check(
    spec: dict[str, Any],
    output: "CaseOutput",
    *,
    case: "Case",
    judge_llm: "LLMClient",
    model_label: str = "",
) -> CheckResult:
    """Score one check against one reply."""
    kind = str(spec.get("type", "")).strip()

    # Refusal checks read the pipeline's own refused flag, not the text, so they
    # are exact: they assert the guard fired (or did not) before any model call.
    if kind == "refused":
        ok = output.refused is True
        return CheckResult(ok, kind, "expected a guard refusal, bot answered" if not ok else "refused")
    if kind == "not_refused":
        ok = output.refused is False
        return CheckResult(
            ok, kind, "guard refused a message that should reach the model" if not ok else "reached the model"
        )

    if kind in _DETERMINISTIC:
        passed, detail = _DETERMINISTIC[kind](output.text, spec)
        return CheckResult(passed, kind, detail)

    if kind == "judge":
        rubric = str(spec.get("rubric", "")).strip()
        passed, reason = await judge_reply(
            judge_llm,
            message=case.message,
            reply=output.text,
            rubric=rubric,
            model_label=model_label,
        )
        return CheckResult(passed, kind, reason)

    return CheckResult(False, kind or "unknown", f"unknown check type {kind!r}")
