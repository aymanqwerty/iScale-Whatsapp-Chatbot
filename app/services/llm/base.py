"""LLM abstraction.

The rest of the application depends on `LLMClient`, never on Groq directly,
so switching provider (or stubbing the model in tests) is a one-line change in
the dependency wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One prior turn of conversation handed to the model as context."""

    #: "assistant" rather than Gemini's "model" - the OpenAI-style vocabulary
    #: Groq expects, and what any future provider is most likely to accept.
    role: Literal["user", "assistant"]
    content: str


@runtime_checkable
class LLMClient(Protocol):
    """Minimal text-in / text-out contract."""

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        history: list[ChatTurn] | None = None,
    ) -> str:
        """Return the model's reply, or raise `LLMError` on failure."""
        ...

    async def health_check(self) -> bool:
        """Cheap liveness probe used by the health endpoint."""
        ...
