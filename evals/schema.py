"""The golden-case data model and its YAML loader.

A case is one user message in one conversation state, plus the checks its reply
must pass. Cases live in ``cases/*.yaml`` as plain data so a reviewer - or a
subject-matter expert who does not read Python - can read, add and argue with
them. Every case carries a ``notes`` field explaining *why* it exists, because a
case whose intent is forgotten is a case nobody dares change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.domain.enums import ConversationState
from app.services.llm.base import ChatTurn

CASES_DIR = Path(__file__).resolve().parent / "cases"


@dataclass(frozen=True, slots=True)
class Case:
    """One scored interaction."""

    id: str
    category: str
    message: str
    state: ConversationState
    checks: tuple[dict[str, Any], ...]
    #: Course slug the user is scoped to, mirroring ``AnswerRequest.course_slug``.
    course: str | None = None
    audience: str = "all"
    #: What the bot already knows about the person (fed back every turn).
    known_profile: str | None = None
    nudge_callback: bool = False
    history: tuple[ChatTurn, ...] = ()
    #: Why this case exists - shown in verbose reports, never scored.
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.checks:
            raise ValueError(f"case {self.id!r} has no checks - it would pass vacuously")


def _coerce_state(raw: Any, case_id: str) -> ConversationState:
    try:
        return ConversationState(str(raw))
    except ValueError as exc:
        valid = ", ".join(s.value for s in ConversationState)
        raise ValueError(
            f"case {case_id!r}: unknown state {raw!r}. Valid states: {valid}"
        ) from exc


def _coerce_history(raw: Any, case_id: str) -> tuple[ChatTurn, ...]:
    if not raw:
        return ()
    turns: list[ChatTurn] = []
    for entry in raw:
        role = str(entry.get("role", "")).strip()
        content = str(entry.get("content", "")).strip()
        if role not in ("user", "assistant"):
            raise ValueError(
                f"case {case_id!r}: history role must be 'user' or 'assistant', got {role!r}"
            )
        turns.append(ChatTurn(role=role, content=content))  # type: ignore[arg-type]
    return tuple(turns)


def _case_from_dict(raw: dict[str, Any], source: Path) -> Case:
    case_id = str(raw.get("id") or "").strip()
    if not case_id:
        raise ValueError(f"{source.name}: a case is missing an 'id'")
    checks = raw.get("checks") or []
    if not isinstance(checks, list):
        raise ValueError(f"case {case_id!r}: 'checks' must be a list")
    return Case(
        id=case_id,
        category=str(raw.get("category") or source.stem),
        message=str(raw.get("message") or ""),
        state=_coerce_state(raw.get("state", "GENERAL_QNA"), case_id),
        checks=tuple(dict(c) for c in checks),
        course=raw.get("course"),
        audience=str(raw.get("audience") or "all"),
        known_profile=raw.get("known_profile"),
        nudge_callback=bool(raw.get("nudge_callback", False)),
        history=_coerce_history(raw.get("history"), case_id),
        notes=str(raw.get("notes") or "").strip(),
    )


def load_cases(directory: Path | None = None) -> list[Case]:
    """Read every ``*.yaml`` under ``directory`` into ``Case`` objects.

    Files are read in sorted order and cases keep their file order, so a report
    reads top to bottom the way the YAML does. Duplicate ids across files are an
    error - a silently shadowed case is a check you think you have and do not.
    """
    directory = directory or CASES_DIR
    if not directory.is_dir():
        raise FileNotFoundError(f"cases directory not found: {directory}")

    cases: list[Case] = []
    seen: dict[str, Path] = {}
    for path in sorted(directory.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = document.get("cases")
        if not isinstance(entries, list):
            raise ValueError(f"{path.name}: expected a top-level 'cases:' list")
        for raw in entries:
            case = _case_from_dict(raw, path)
            if case.id in seen:
                raise ValueError(
                    f"duplicate case id {case.id!r} in {path.name} "
                    f"(already defined in {seen[case.id].name})"
                )
            seen[case.id] = path
            cases.append(case)

    if not cases:
        raise ValueError(f"no cases found under {directory}")
    return cases
