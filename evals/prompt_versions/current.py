"""Current prompt version -- a thin re-export of the shipping prompt.

Importing straight from ``app.services.llm.prompts`` means the eval always scores
the prompt that actually ships. There is no second copy to drift out of sync: if
someone edits the real prompt, the next eval run reflects it automatically.
"""

from __future__ import annotations

from app.services.llm.prompts import (  # noqa: F401  (re-exported on purpose)
    build_system_prompt,
    build_user_prompt,
)

VERSION = "current"
DESCRIPTION = (
    "Live prompt from app/services/llm/prompts.py: fees quotable from KNOWLEDGE, "
    "explicit anti-false-booking rule, discovery/upsell guidance tailored to the "
    "person, discount invention forbidden."
)
