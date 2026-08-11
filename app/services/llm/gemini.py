"""Gemini client.

Talks to the Generative Language REST API over plain `httpx`, mirroring the Groq
client so the two are interchangeable behind `LLMClient`. No Google SDK: it
would pull in a large dependency tree for one POST, and the auth here is a
single header.

Three things differ from the OpenAI-shaped API and each one is handled below:

* the system prompt is its own `systemInstruction` field, not a message;
* the assistant role is called `model`;
* thinking is billed as output tokens, and is on by default.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, LLMError
from app.core.logging import get_logger
from app.services.llm.base import ChatTurn

logger = get_logger(__name__)

#: Transient statuses worth a retry: rate limits and upstream hiccups.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

#: Finish reasons that mean the answer was cut off or withheld rather than
#: completed. Surfaced explicitly because the response still parses as valid
#: JSON with an empty body - which would otherwise look like a model failure.
_BAD_FINISH = {
    "SAFETY": "The model declined that on safety grounds.",
    "RECITATION": "The model declined that to avoid reciting source material.",
    "PROHIBITED_CONTENT": "The model declined that as prohibited content.",
    "BLOCKLIST": "The model declined that as blocked content.",
}


class GeminiClient:
    """Async text generation against the Gemini REST API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._model = settings.gemini_model
        self._base_url = settings.gemini_base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.gemini_timeout_seconds
        )

    # ------------------------------------------------------------------ #
    @property
    def _endpoint(self) -> str:
        return f"{self._base_url}/models/{self._model}:generateContent"

    def _headers(self) -> dict[str, str]:
        api_key = self._settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not set")
        # Header rather than ?key= so the secret never lands in a proxy access
        # log or an exception message containing the URL.
        return {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    # ------------------------------------------------------------------ #
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(LLMError),
        reraise=True,
    )
    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        history: list[ChatTurn] | None = None,
    ) -> str:
        headers = self._headers()

        contents: list[dict[str, Any]] = []
        for turn in history or []:
            if turn.content.strip():
                # Gemini calls the assistant "model"; everything above this
                # module speaks the OpenAI vocabulary.
                role = "model" if turn.role == "assistant" else "user"
                contents.append({"role": role, "parts": [{"text": turn.content}]})
        contents.append({"role": "user", "parts": [{"text": user_prompt}]})

        generation: dict[str, Any] = {
            "temperature": self._settings.gemini_temperature,
            "maxOutputTokens": self._settings.gemini_max_output_tokens,
            # One candidate keeps latency and cost down; grounding comes from the
            # prompt and the low temperature, not from sampling more options.
            "candidateCount": 1,
        }
        if self._settings.gemini_thinking_budget >= 0:
            # Thinking tokens are billed as output and are ON by default. On a
            # measured one-line reply they were 174 of 206 tokens - four times
            # the quota for an answer that is already fully grounded by the
            # knowledge section. Zero disables it.
            generation["thinkingConfig"] = {
                "thinkingBudget": self._settings.gemini_thinking_budget
            }

        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": generation,
        }

        try:
            response = await self._client.post(
                self._endpoint, json=payload, headers=headers
            )
        except httpx.TimeoutException as exc:
            logger.warning("Gemini request timed out")
            raise LLMError("The model took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"HTTP error calling Gemini: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUS:
            raise LLMError(
                f"Gemini returned {response.status_code}: {_error_detail(response)}"
            )

        if response.is_error:
            # 4xx other than rate limiting is a bad request - a bad model name or
            # an invalid key. Retrying cannot fix it, so fail with Google's detail.
            detail = _error_detail(response)
            logger.error(
                "Gemini rejected the request",
                extra={"status": response.status_code, "detail": detail},
            )
            raise ConfigurationError(f"Gemini rejected the request: {detail}")

        text = self._extract_text(response)
        if not text:
            logger.warning("Gemini returned an empty completion")
            raise LLMError("The model returned an empty response.")
        return text

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_text(response: httpx.Response) -> str:
        """Pull the reply out, or raise with a reason the caller can act on."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMError("Gemini returned a non-JSON body") from exc

        # A prompt blocked before generation has no candidates at all.
        feedback = payload.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise LLMError(
                f"Gemini blocked the prompt: {feedback['blockReason']}"
            )

        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return ""
        candidate = candidates[0] if isinstance(candidates[0], dict) else {}

        reason = str(candidate.get("finishReason") or "")
        if reason in _BAD_FINISH:
            raise LLMError(_BAD_FINISH[reason])
        if reason == "MAX_TOKENS":
            # Not fatal on its own - whatever was produced is still usable, and
            # a truncated answer beats no answer. Logged so a persistently low
            # limit is visible.
            logger.warning("Gemini hit maxOutputTokens; the reply may be cut off")

        parts = candidate.get("content", {}).get("parts")
        if not isinstance(parts, list):
            return ""
        text = "".join(
            part.get("text", "") for part in parts if isinstance(part, dict)
        )
        return text.strip()

    async def health_check(self) -> bool:
        try:
            self._headers()
        except ConfigurationError:
            return False
        return True

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return f"{error.get('status', error.get('code'))}: {error.get('message')}"
    return str(payload)[:300]
