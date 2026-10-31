"""Print a fully-assembled prompt as plain text.

The main prompt is assembled per turn from several pieces, so "the prompt" is not
one static string - it depends on the state, the course in scope and what the bot
already knows. This prints the exact text the model receives for one
representative turn, for either prompt version. Useful two ways:

* on camera (assignment point 8: "show the prompts on screen") - pipe it to a
  file or a pager and scroll the real thing;
* for the ZIP - ``python -m evals.render_prompt --version current > current.txt``
  and ``--version v1_initial > first.txt`` give the two prompt deliverables as
  rendered text, beside the source in ``prompt_versions/``.

No model is called; this is pure assembly.
"""

from __future__ import annotations

import argparse
import sys

from app.domain.enums import ConversationState
from app.services.knowledge.loader import load_knowledge_base
from app.services.knowledge.retriever import build_retriever
from app.core.config import Settings
from evals.pipeline import _build_prompts
from evals.prompt_versions import KNOWN_VERSIONS, load_version
from evals.schema import Case


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.render_prompt",
        description="Print the assembled system + user prompt for one turn.",
    )
    parser.add_argument("--version", default="current", help=f"one of: {', '.join(KNOWN_VERSIONS)}")
    parser.add_argument("--state", default="COURSE_QNA", help="conversation state")
    parser.add_argument("--course", default="ai-engineer-advance-program", help="course slug in scope")
    parser.add_argument(
        "--message",
        default="How much does the AI Engineer Advance Program cost and what will I learn?",
        help="the user message to wrap",
    )
    parser.add_argument("--known-profile", default=None, help="what the bot already knows")
    parser.add_argument("--system-only", action="store_true", help="print only the system prompt")
    return parser.parse_args()


def main() -> int:
    # The discovery prompt section contains emoji; make stdout UTF-8 so printing
    # (or piping to a file) never dies on the Windows console codec.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover
                pass
    args = _parse_args()
    settings = Settings()
    kb = load_knowledge_base(settings.knowledge_dir)
    retriever = build_retriever(
        kb, limit=settings.knowledge_max_snippets, max_chars=settings.knowledge_max_chars
    )
    module = load_version(args.version)

    case = Case(
        id="render",
        category="render",
        message=args.message,
        state=ConversationState(args.state),
        checks=({"type": "not_refused"},),
        course=args.course,
        known_profile=args.known_profile,
    )
    snippets = retriever.retrieve(case.message, course=case.course)  # type: ignore[arg-type]
    system_prompt, user_prompt = _build_prompts(module, kb=kb, case=case, snippets=snippets)

    bar = "=" * 72
    print(bar)
    print(f"PROMPT VERSION: {args.version}   STATE: {args.state}   COURSE: {args.course}")
    print(f"retrieved snippets: {', '.join(s.id for s in snippets) or '(none)'}")
    print(bar)
    print("\n----- SYSTEM PROMPT -----\n")
    print(system_prompt)
    if not args.system_only:
        print("\n----- USER PROMPT (knowledge + message) -----\n")
        print(user_prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
