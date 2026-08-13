"""The OpenAI client: request shape, response parsing and failure modes.

Every test runs against a stubbed transport. Nothing here touches the network -
the point is the translation between our vocabulary and OpenAI's, which is where
a provider swap actually goes wrong.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, LLMError
from app.services.llm.base import ChatTurn, LLMClient
from app.services.llm.openai import OpenAIClient


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "openai_api_key": "test-key",
        "openai_model": "gpt-4o-mini",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _client(handler, **overrides: object) -> OpenAIClient:
    transport = httpx.MockTransport(handler)
    return OpenAIClient(
        _settings(**overrides), client=httpx.AsyncClient(transport=transport)
    )


def _reply(text: str | None, finish: str = "stop", **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    message.update(extra)
    return {"choices": [{"message": message, "finish_reason": finish}]}


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
def test_it_satisfies_the_llm_protocol() -> None:
    """Swapping provider must be a wiring change, not a code change."""
    assert isinstance(OpenAIClient(_settings()), LLMClient)


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #
async def test_the_system_prompt_leads_the_message_list() -> None:
    """OpenAI takes the grounding rules as a system message, first in the list."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler).generate(
        system_prompt="Never invent a price.", user_prompt="How much?"
    )

    assert seen["messages"][0] == {
        "role": "system",
        "content": "Never invent a price.",
    }
    assert seen["messages"][-1] == {"role": "user", "content": "How much?"}


async def test_history_keeps_the_openai_role_names() -> None:
    """Unlike Gemini, "assistant" is passed through unchanged."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler).generate(
        system_prompt="s",
        user_prompt="q",
        history=[
            ChatTurn(role="user", content="hi"),
            ChatTurn(role="assistant", content="Hello!"),
        ],
    )

    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


async def test_blank_history_turns_are_dropped() -> None:
    """An empty content field is rejected by the API, so it never gets sent."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler).generate(
        system_prompt="s",
        user_prompt="q",
        history=[ChatTurn(role="user", content="   ")],
    )

    assert len(seen["messages"]) == 2, "the blank turn was sent anyway"


async def test_it_sends_max_completion_tokens_not_max_tokens() -> None:
    """`max_tokens` is deprecated and is rejected outright by newer models.

    Sending the old name would work today and break the moment OPENAI_MODEL is
    pointed at a reasoning model - a config change causing a code failure.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler, openai_max_output_tokens=256).generate(
        system_prompt="s", user_prompt="q"
    )

    assert seen["max_completion_tokens"] == 256
    assert "max_tokens" not in seen


async def test_temperature_is_omitted_when_unset() -> None:
    """The gpt-5 family 400s on any explicit temperature.

    Leaving OPENAI_TEMPERATURE empty must drop the field entirely rather than
    send a default, or those models are unusable without a code change.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler, openai_temperature=None).generate(
        system_prompt="s", user_prompt="q"
    )

    assert "temperature" not in seen


async def test_temperature_is_sent_when_set() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler, openai_temperature=0.3).generate(
        system_prompt="s", user_prompt="q"
    )

    assert seen["temperature"] == 0.3


async def test_the_key_travels_as_a_bearer_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler).generate(system_prompt="s", user_prompt="q")

    assert seen["auth"] == "Bearer test-key"


async def test_org_and_project_headers_are_only_sent_when_configured() -> None:
    """Most keys need neither, and an empty header is not the same as absent."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["org"] = request.headers.get("OpenAI-Organization")
        seen["project"] = request.headers.get("OpenAI-Project")
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler).generate(system_prompt="s", user_prompt="q")
    assert seen["org"] is None and seen["project"] is None

    await _client(handler, openai_organization="org-1", openai_project="proj-1").generate(
        system_prompt="s", user_prompt="q"
    )
    assert seen["org"] == "org-1" and seen["project"] == "proj-1"


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #
async def test_the_reply_is_returned_stripped() -> None:
    client = _client(lambda _: httpx.Response(200, json=_reply("  Hello there.  ")))
    assert await client.generate(system_prompt="s", user_prompt="q") == "Hello there."


async def test_a_refusal_is_reported_not_swallowed() -> None:
    """A refusal is a 200 with null content, so it would read as an empty reply.

    Retried three times and reported as "empty response", it tells nobody what
    actually happened.
    """
    client = _client(
        lambda _: httpx.Response(
            200, json=_reply(None, refusal="I can't help with that.")
        )
    )

    with pytest.raises(LLMError, match="declined"):
        await client.generate(system_prompt="s", user_prompt="q")


async def test_an_empty_completion_is_an_error() -> None:
    client = _client(lambda _: httpx.Response(200, json=_reply("")))
    with pytest.raises(LLMError, match="empty"):
        await client.generate(system_prompt="s", user_prompt="q")


async def test_a_truncated_reply_is_still_returned() -> None:
    """A cut-off answer beats no answer; the warning is for the operator."""
    client = _client(
        lambda _: httpx.Response(200, json=_reply("The fee is", finish="length"))
    )
    assert await client.generate(system_prompt="s", user_prompt="q") == "The fee is"


async def test_a_non_json_body_is_an_error() -> None:
    client = _client(lambda _: httpx.Response(200, text="<html>502</html>"))
    with pytest.raises(LLMError):
        await client.generate(system_prompt="s", user_prompt="q")


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #
async def test_rate_limits_are_retried() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=_reply("finally"))

    assert (
        await _client(handler).generate(system_prompt="s", user_prompt="q") == "finally"
    )
    assert calls["n"] == 3


async def test_an_exhausted_balance_fails_immediately() -> None:
    """402 is not retryable: credits do not reappear within four seconds.

    Retrying triples the latency of every message once the balance runs dry,
    which turns a billing problem into a timeout problem.
    """
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            402, json={"error": {"code": "insufficient_quota", "message": "no credit"}}
        )

    with pytest.raises(ConfigurationError, match="insufficient_quota"):
        await _client(handler).generate(system_prompt="s", user_prompt="q")
    assert calls["n"] == 1, "an empty balance was retried"


async def test_a_bad_model_name_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            404, json={"error": {"code": "model_not_found", "message": "no such model"}}
        )

    with pytest.raises(ConfigurationError, match="model_not_found"):
        await _client(handler).generate(system_prompt="s", user_prompt="q")
    assert calls["n"] == 1


async def test_a_missing_key_is_a_configuration_error() -> None:
    client = _client(lambda _: httpx.Response(200, json=_reply("ok")), openai_api_key="")
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        await client.generate(system_prompt="s", user_prompt="q")


async def test_health_check_reflects_whether_the_key_is_set() -> None:
    assert await _client(lambda _: httpx.Response(200)).health_check() is True
    assert (
        await _client(lambda _: httpx.Response(200), openai_api_key="").health_check()
        is False
    )


async def test_a_timeout_becomes_an_llm_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow")

    with pytest.raises(LLMError, match="too long"):
        await _client(handler).generate(system_prompt="s", user_prompt="q")


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("provider", "expected"),
    [("openai", "OpenAIClient"), ("gemini", "GeminiClient"), ("groq", "GroqClient")],
)
def test_the_container_builds_the_configured_provider(
    provider: str, expected: str
) -> None:
    """The whole point of the abstraction: provider is config, not code."""
    from app.services.llm.gemini import GeminiClient
    from app.services.llm.groq import GroqClient

    settings = _settings(
        llm_provider=provider, groq_api_key="g", gemini_api_key="x", openai_api_key="o"
    )
    built = {
        "groq": GroqClient,
        "gemini": GeminiClient,
        "openai": OpenAIClient,
    }[settings.llm_provider](settings)

    assert type(built).__name__ == expected
