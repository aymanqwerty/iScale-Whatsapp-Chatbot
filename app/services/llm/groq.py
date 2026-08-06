"""Groq client.

Groq exposes an OpenAI-compatible chat-completions endpoint, so this talks to it
over plain `httpx` rather than pulling in another SDK. That keeps the dependency
list short and reuses the timeout/retry approach already used for the WhatsApp
client.

Only `LLMClient` is implemented here; nothing above this module knows the
provider.
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


class GroqClient:
    """Async chat completions against the Groq API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._model = settings.groq_model
        self._base_url = settings.groq_base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=settings.groq_timeout_seconds)

    # ------------------------------------------------------------------ #
    @property
    def _endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        api_key = self._settings.groq_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GROQ_API_KEY is not set")
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

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
            "temperature": self._settings.groq_temperature,
            "max_tokens": self._settings.groq_max_output_tokens,
            # One candidate keeps latency and cost down; grounding comes from the
            # prompt and the low temperature, not from sampling more options.
            "n": 1,
            "stream": False,
        }

        try:
            response = await self._client.post(
                self._endpoint, json=payload, headers=headers
            )
        except httpx.TimeoutException as exc:
            logger.warning("Groq request timed out")
            raise LLMError("The model took too long to respond.") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"HTTP error calling Groq: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUS:
            raise LLMError(
                f"Groq returned {response.status_code}: {_error_detail(response)}"
            )

        if response.is_error:
            # 4xx other than rate limiting is a bad request - a bad model name or
            # an invalid key. Retrying cannot fix it, so fail with Groq's detail.
            detail = _error_detail(response)
            logger.error(
                "Groq rejected the request",
                extra={"status": response.status_code, "detail": detail},
            )
            raise ConfigurationError(f"Groq rejected the request: {detail}")

        text = self._extract_text(response)
        if not text:
            logger.warning("Groq returned an empty completion")
            raise LLMError("The model returned an empty response.")
        return text

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_text(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMError("Groq returned a non-JSON body") from exc

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            return ""
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
        return f"{error.get('code')}: {error.get('message')}"
    return str(payload)[:300]
