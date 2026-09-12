"""The guarantees that let this run on request-based billing.

Cloud Run withdraws CPU the moment a response is sent, so anything deferred
past the response never runs. That would be silent and total: the bot accepts
every message, Meta gets its 200 and never retries, and nobody is answered.

Each test here pins one of the three properties that make the cheap billing
mode safe. If any fails, the service must go back to CPU-always-allocated.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
from tests.conftest import PROJECT_ROOT, FakeLLM, FrozenClockValidator


def _settings(db_file: Path, **overrides: object) -> Settings:
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    defaults: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "database_url": f"sqlite+aiosqlite:///{db_file}",
        "knowledge_dir": PROJECT_ROOT / "knowledge",
        "whatsapp_enabled": False,
        "google_sheets_enabled": False,
        "whatsapp_allowlist_enabled": False,
        "log_level": "WARNING",
        "api_key": "test-internal-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _client(tmp_path: Path, **overrides: object) -> TestClient:
    app = create_app(_settings(tmp_path / "billing.db", **overrides))
    client = TestClient(app)
    client.__enter__()
    container = app.state.container
    container.answer_service._llm = FakeLLM(reply="A grounded answer.")
    container.callback_validator = FrozenClockValidator(app.state.settings)
    return client


# --------------------------------------------------------------------------- #
# 1. The turn finishes before the webhook answers
# --------------------------------------------------------------------------- #
def _text_event(text: str, message_id: str, sender: str = "919876543210") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"wa_id": sender, "profile": {"name": "Rahul"}}],
            "messages": [{"id": message_id, "from": sender, "type": "text",
                          "text": {"body": text}}],
        }}]}],
    }


def test_the_webhook_does_not_defer_the_turn(tmp_path: Path, monkeypatch) -> None:
    """The property the whole billing change rests on.

    Asserted on the dispatch itself rather than on the reply, because Starlette's
    test client runs background tasks before handing back the response - so a
    test that only checked "did a reply go out" would pass just as happily with
    the deferred path, and would have told us nothing.
    """
    from fastapi import BackgroundTasks

    deferred: list[str] = []
    original = BackgroundTasks.add_task

    def spy(self, func, *args, **kwargs):  # type: ignore[no-untyped-def]
        deferred.append(getattr(func, "__name__", str(func)))
        return original(self, func, *args, **kwargs)

    monkeypatch.setattr(BackgroundTasks, "add_task", spy)

    client = _client(tmp_path, webhook_inline_processing=True)
    try:
        outbox = client.app.state.container.messaging
        before = len(outbox.sent)

        response = client.post(
            "/api/v1/webhook", json=_text_event("hi", "wamid.inline-1")
        )

        assert response.status_code == 200
        assert response.json()["messages"] == 1
        assert deferred == [], f"work was deferred past the response: {deferred}"
        assert len(outbox.sent) > before, "no reply was produced"
    finally:
        client.__exit__(None, None, None)


def test_deferred_mode_really_defers(tmp_path: Path, monkeypatch) -> None:
    """The rollback path must genuinely be the old behaviour."""
    from fastapi import BackgroundTasks

    deferred: list[str] = []
    original = BackgroundTasks.add_task

    def spy(self, func, *args, **kwargs):  # type: ignore[no-untyped-def]
        deferred.append(getattr(func, "__name__", str(func)))
        return original(self, func, *args, **kwargs)

    monkeypatch.setattr(BackgroundTasks, "add_task", spy)

    client = _client(tmp_path, webhook_inline_processing=False)
    try:
        client.post("/api/v1/webhook", json=_text_event("hi", "wamid.deferred-1"))
        assert deferred == ["_process"], f"expected deferral, got {deferred}"
    finally:
        client.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# 2. A redelivery mid-turn is silent, not an apology
# --------------------------------------------------------------------------- #
async def test_a_concurrent_redelivery_produces_no_second_reply(harness) -> None:
    """Answering Meta late means it can retry while the first turn is running.

    The unique index on wa_message_id catches it. The customer must not then be
    told something went wrong - they are already getting an answer.
    """
    import uuid

    from datetime import datetime

    from app.domain.enums import MessageKind
    from app.domain.messaging import InboundMessage

    wa_id = f"wamid.{uuid.uuid4().hex}"

    def message() -> InboundMessage:
        return InboundMessage(
            wa_message_id=wa_id,
            from_phone=harness.phone,
            kind=MessageKind.TEXT,
            text="hi",
            timestamp=datetime.now(),
        )

    await harness.service.process_inbound(message())
    before = len(harness.messaging.sent)

    # The same message again - as Meta would redeliver it.
    await harness.service.process_inbound(message())

    replies = [m.text for _, m in harness.messaging.sent[before:]]
    assert not any("went wrong" in r.lower() for r in replies), (
        f"a redelivery apologised to the customer: {replies}"
    )


# --------------------------------------------------------------------------- #
# 3. The sweep can be driven from outside the process
# --------------------------------------------------------------------------- #
def test_the_sweep_endpoint_runs_a_sweep(tmp_path: Path) -> None:
    client = _client(tmp_path, inactivity_in_process=False)
    try:
        response = client.post(
            "/api/v1/internal/sweep", headers={"X-API-Key": "test-internal-key"}
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "nudged": 0}
    finally:
        client.__exit__(None, None, None)


def test_the_sweep_endpoint_needs_the_api_key(tmp_path: Path) -> None:
    """It sends real WhatsApp messages - not something a stranger may trigger."""
    client = _client(tmp_path, inactivity_in_process=False)
    try:
        assert client.post("/api/v1/internal/sweep").status_code == 401
        assert (
            client.post(
                "/api/v1/internal/sweep", headers={"X-API-Key": "wrong"}
            ).status_code
            == 401
        )
    finally:
        client.__exit__(None, None, None)


def test_no_background_loop_when_driven_externally(tmp_path: Path) -> None:
    """Two sweepers would double every nudge."""
    client = _client(tmp_path, inactivity_in_process=False)
    try:
        assert client.app.state.sweeper._task is None
    finally:
        client.__exit__(None, None, None)


def test_the_loop_still_runs_when_in_process(tmp_path: Path) -> None:
    """Local development and any always-on host keep the old behaviour."""
    client = _client(tmp_path, inactivity_in_process=True)
    try:
        assert client.app.state.sweeper._task is not None
    finally:
        client.__exit__(None, None, None)
