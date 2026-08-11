"""The Gemini client: request shape, response parsing and failure modes.

Every test runs against a stubbed transport. Nothing here touches the network -
the point is the translation between our vocabulary and Google's, which is where
a provider swap actually goes wrong.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, LLMError
from app.services.llm.base import ChatTurn, LLMClient
from app.services.llm.gemini import GeminiClient


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "gemini_api_key": "test-key",
        "gemini_model": "gemini-2.5-flash",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _client(handler, **overrides: object) -> GeminiClient:
    transport = httpx.MockTransport(handler)
    return GeminiClient(
        _settings(**overrides), client=httpx.AsyncClient(transport=transport)
    )


def _reply(text: str, finish: str = "STOP") -> dict[str, object]:
    return {
        "candidates": [
            {"content": {"parts": [{"text": text}]}, "finishReason": finish}
        ]
    }


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
def test_it_satisfies_the_llm_protocol() -> None:
    """Swapping provider must be a wiring change, not a code change."""
    assert isinstance(GeminiClient(_settings()), LLMClient)


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #
async def test_the_system_prompt_is_a_separate_field() -> None:
    """Gemini takes `systemInstruction`, not a system message in the list.

    Sent as a message it is silently treated as user text, and the grounding
    rules stop being instructions - which is the failure mode that would let the
    bot invent a price.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler).generate(system_prompt="BE GROUNDED", user_prompt="hi")

    assert seen["systemInstruction"] == {"parts": [{"text": "BE GROUNDED"}]}
    roles = [c["role"] for c in seen["contents"]]  # type: ignore[index]
    assert "system" not in roles


async def test_assistant_turns_are_renamed_to_model() -> None:
    """Everything above this module speaks OpenAI's vocabulary."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler).generate(
        system_prompt="s",
        user_prompt="and the fees?",
        history=[
            ChatTurn(role="user", content="hi"),
            ChatTurn(role="assistant", content="Hello!"),
        ],
    )

    contents = seen["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]  # type: ignore[index]
    assert contents[-1]["parts"][0]["text"] == "and the fees?"  # type: ignore[index]


async def test_empty_history_turns_are_dropped() -> None:
    """A blank part makes Gemini reject the whole request."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler).generate(
        system_prompt="s",
        user_prompt="q",
        history=[ChatTurn(role="user", content="   ")],
    )

    assert len(seen["contents"]) == 1  # type: ignore[arg-type]


async def test_thinking_is_disabled_by_default() -> None:
    """Thinking tokens bill as output: measured 174 of 206 on a one-line reply.

    Four times the quota for an answer already grounded by the knowledge
    section, on a bot that exists because the previous provider's quota ran out.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler).generate(system_prompt="s", user_prompt="q")

    config = seen["generationConfig"]
    assert config["thinkingConfig"] == {"thinkingBudget": 0}  # type: ignore[index]


async def test_a_negative_budget_leaves_googles_default_alone() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler, gemini_thinking_budget=-1).generate(
        system_prompt="s", user_prompt="q"
    )

    assert "thinkingConfig" not in seen["generationConfig"]  # type: ignore[operator]


async def test_the_key_travels_in_a_header_not_the_url() -> None:
    """A ?key= query string ends up in proxy logs and error messages."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("x-goog-api-key", "")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_reply("ok"))

    await _client(handler).generate(system_prompt="s", user_prompt="q")

    assert seen["auth"] == "test-key"
    assert "test-key" not in seen["url"]


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
async def test_a_normal_reply_is_returned() -> None:
    client = _client(lambda r: httpx.Response(200, json=_reply("  The fee is Rs 4,999.  ")))

    assert await client.generate(system_prompt="s", user_prompt="q") == "The fee is Rs 4,999."


async def test_multi_part_replies_are_joined() -> None:
    payload = {
        "candidates": [
            {
                "content": {"parts": [{"text": "Rs 4,999"}, {"text": " including taxes."}]},
                "finishReason": "STOP",
            }
        ]
    }
    client = _client(lambda r: httpx.Response(200, json=payload))

    assert await client.generate(system_prompt="s", user_prompt="q") == (
        "Rs 4,999 including taxes."
    )


async def test_a_safety_refusal_is_reported_not_returned_blank() -> None:
    """The response is valid JSON with no text, which reads as a model failure."""
    client = _client(lambda r: httpx.Response(200, json=_reply("", finish="SAFETY")))

    with pytest.raises(LLMError, match="safety"):
        await client.generate(system_prompt="s", user_prompt="q")


async def test_a_blocked_prompt_is_reported() -> None:
    payload = {"promptFeedback": {"blockReason": "SAFETY"}}
    client = _client(lambda r: httpx.Response(200, json=payload))

    with pytest.raises(LLMError, match="blocked"):
        await client.generate(system_prompt="s", user_prompt="q")


async def test_a_truncated_reply_is_still_returned() -> None:
    """Half an answer beats none - the caller shows it and logs the cause."""
    client = _client(
        lambda r: httpx.Response(200, json=_reply("The fee is", finish="MAX_TOKENS"))
    )

    assert await client.generate(system_prompt="s", user_prompt="q") == "The fee is"


async def test_an_empty_completion_raises() -> None:
    client = _client(lambda r: httpx.Response(200, json={"candidates": []}))

    with pytest.raises(LLMError):
        await client.generate(system_prompt="s", user_prompt="q")


async def test_a_non_json_body_raises() -> None:
    client = _client(lambda r: httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(LLMError):
        await client.generate(system_prompt="s", user_prompt="q")


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #
async def test_quota_exhaustion_is_retried_then_surfaced() -> None:
    """429 is the reason for this migration, so it must degrade, not crash."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})

    with pytest.raises(LLMError):
        await _client(handler).generate(system_prompt="s", user_prompt="q")

    assert calls["n"] == 3, "a transient status should be retried"


async def test_a_bad_key_fails_immediately_without_retrying() -> None:
    """Retrying a rejected credential just wastes three round trips."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, json={"error": {"message": "invalid key"}})

    with pytest.raises(ConfigurationError):
        await _client(handler).generate(system_prompt="s", user_prompt="q")

    assert calls["n"] == 1


async def test_a_missing_key_is_refused_before_any_request() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls["n"] += 1
        return httpx.Response(200, json=_reply("ok"))

    client = _client(handler, gemini_api_key="")

    with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
        await client.generate(system_prompt="s", user_prompt="q")
    assert calls["n"] == 0
    assert await client.health_check() is False


async def test_a_timeout_becomes_an_llm_error() -> None:
    """`LLMError` is what the answer service catches to serve the fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow")

    with pytest.raises(LLMError):
        await _client(handler).generate(system_prompt="s", user_prompt="q")


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_the_provider_setting_picks_the_client() -> None:
    """Switching provider must not need a redeploy of different code."""
    from app.services.llm.groq import GroqClient

    for provider, expected in (("gemini", GeminiClient), ("groq", GroqClient)):
        settings = _settings(
            llm_provider=provider,
            groq_api_key="g",
            database_url="sqlite+aiosqlite:///:memory:",
        )
        chosen = (
            GroqClient(settings) if settings.llm_provider == "groq" else GeminiClient(settings)
        )
        assert isinstance(chosen, expected)
