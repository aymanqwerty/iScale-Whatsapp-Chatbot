"""OpenAI client.

Chat completions over plain `httpx`, like the other two providers - no SDK for
one POST. The request shape is the same OpenAI-compatible one the Groq client
uses, which is why that file and this one look alike; they are kept separate
rather than shared because the two diverge on exactly the details below, and a
single client with provider branches would be harder to reason about than two
short ones.

Three things differ from Groq's older-style API and each is handled here:

* `max_completion_tokens`, not `max_tokens`. The latter is deprecated and is
  rejected outright by the reasoning-capable models, so using the new name is
  what keeps this working when the model is changed by an env var alone.
* `temperature` is omitted when unset. The gpt-5 family accepts only the
  default and 400s on anything else, so `OPENAI_TEMPERATURE=` (empty) is the
  escape hatch that makes those models usable without a code change.
* A refusal arrives as a `refusal` field beside `content` rather than as an
  error, and would otherwise read as an empty completion.
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

#: Transient statuses worth a retry: rate limits and upstream hiccups. 402 is
#: not among them - an exhausted credit balance does not recover in four
#: seconds, and retrying it three times just triples the latency of the failure.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class OpenAIClient:
    """Async chat completions against the OpenAI API."""

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        self._model = settings.openai_model
        self._base_url = settings.openai_base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.openai_timeout_seconds
        )

    # ------------------------------------------------------------------ #
    @property
    def _endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        api_key = self._settings.openai_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("OPENAI_API_KEY is not set")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        # Only sent when set. Keys belonging to several organisations or
        # projects are ambiguous otherwise, and the resulting 401 says nothing
        # about which of the two is missing.
        if self._settings.openai_organization:
            headers["OpenAI-Organization"] = self._settings.openai_organization
        if self._settings.openai_project:
            headers["OpenAI-Project"] = self._settings.openai_project
        return headers

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

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for turn in history or []:
            if turn.content.strip():
                messages.append({"role": turn.role, "content": turn.content})
        messages.append({"role": "user", "content": user_prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_completion_tokens": self._settings.openai_max_output_tokens,
            # One candidate keeps latency and cost down; grounding comes from the
            # prompt and the low temperature, not from sampling more options.
            "n": 1,
            "stream": False,
        }
        if self._settings.openai_temperature is not None:
            payload["temperature"] = self._settings.openai_temperature

        try:
            response = await self._client.post(
                self._endpoint, json=payload, headers=headers
            )
        except httpx.TimeoutException as exc:
            logger.warning("OpenAI request timed out")
            raise LLMError("The model took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"HTTP error calling OpenAI: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUS:
            raise LLMError(
                f"OpenAI returned {response.status_code}: {_error_detail(response)}"
            )

        if response.is_error:
            # 4xx other than rate limiting is a bad request - a bad model name,
            # an invalid key, or an empty credit balance. Retrying cannot fix any
            # of them, so fail with OpenAI's own detail, which names which.
            detail = _error_detail(response)
            logger.error(
                "OpenAI rejected the request",
                extra={"status": response.status_code, "detail": detail},
            )
            raise ConfigurationError(f"OpenAI rejected the request: {detail}")

        text = self._extract_text(response)
        if not text:
            logger.warning("OpenAI returned an empty completion")
            raise LLMError("The model returned an empty response.")
        return text

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_text(response: httpx.Response) -> str:
        """Pull the reply out, or raise with a reason the caller can act on."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMError("OpenAI returned a non-JSON body") from exc

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        if not isinstance(message, dict):
            return ""

        # A safety refusal is a 200 with `content: null`, so without this it
        # would surface as "the model returned an empty response" and be retried
        # three times before failing with a message that explains nothing.
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            raise LLMError(f"The model declined that: {refusal.strip()}")

        # `length` means the answer was cut off mid-sentence. Not fatal - a
        # truncated answer beats no answer - but logged so a persistently low
        # limit is visible rather than looking like the model being terse.
        if choice.get("finish_reason") == "length":
            logger.warning(
                "OpenAI hit max_completion_tokens; the reply may be cut off"
            )

        content = message.get("content")
        return content.strip() if isinstance(content, str) else ""

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
        return f"{error.get('code') or error.get('type')}: {error.get('message')}"
    return str(payload)[:300]
