"""HTTP-level tests: webhook contract, health, simulator and lead endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.bot import copy
from app.core.config import Settings
from app.db.base import Base
from app.domain.messaging import OutboundMessage
from app.main import create_app
from tests.conftest import PROJECT_ROOT, FakeLLM, FrozenClockValidator

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
PHONE = "919876543210"
#: A number the developer never allowlisted - stands in for a real customer.
STRANGER = "918888800000"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_file = tmp_path / "api.db"

    # Create the schema up front with a synchronous engine; the app itself only
    # ever uses the async one.
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{db_file}",
        knowledge_dir=PROJECT_ROOT / "knowledge",
        whatsapp_enabled=False,
        google_sheets_enabled=False,
        whatsapp_app_secret=APP_SECRET,
        whatsapp_verify_token=VERIFY_TOKEN,
        whatsapp_allowlist_enabled=True,
        whatsapp_allowed_numbers=PHONE,
        log_level="WARNING",
    )

    app = create_app(settings)
    with TestClient(app) as test_client:
        container = app.state.container
        # Replace the two network boundaries only; everything else is real.
        container.answer_service._llm = FakeLLM(reply="A grounded answer.")
        container.callback_validator = FrozenClockValidator(settings)
        yield test_client


def _user_rows(client: TestClient, phone: str) -> int:
    """Count user rows for a number, over a plain sqlite3 connection.

    Deliberately not through the app: the simulator would *create* the very row
    this test is asserting the absence of, and the async engine belongs to the
    TestClient's event loop.
    """
    url = client.app.state.container.settings.database_url  # type: ignore[attr-defined]
    db_path = url.split("///", 1)[1]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM users WHERE phone = ?", (phone,)
        ).fetchone()
    return int(rows[0])


def _signed(body: dict[str, object]) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }


def _text_event(
    text: str, message_id: str, *, sender: str = PHONE
) -> dict[str, object]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [
                                {"wa_id": sender, "profile": {"name": "Rahul"}}
                            ],
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": sender,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def test_health_is_cheap_and_always_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_each_dependency(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"]["database"] == "ok"
    assert payload["checks"]["knowledge_base"]["courses"] == 8


def test_root_reports_the_service(client: TestClient) -> None:
    assert client.get("/").json()["service"]


# --------------------------------------------------------------------------- #
# Webhook verification handshake
# --------------------------------------------------------------------------- #
def test_verification_echoes_the_challenge(client: TestClient) -> None:
    response = client.get(
        "/api/v1/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1234567890",
        },
    )

    assert response.status_code == 200
    # Meta requires the raw challenge, not a JSON document.
    assert response.text == "1234567890"


def test_verification_rejects_a_wrong_token(client: TestClient) -> None:
    response = client.get(
        "/api/v1/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "1234567890",
        },
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Webhook delivery
# --------------------------------------------------------------------------- #
def test_unsigned_request_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/webhook", json=_text_event("hi", "wamid.1"))

    assert response.status_code == 403


def test_tampered_signature_is_rejected(client: TestClient) -> None:
    raw, headers = _signed(_text_event("hi", "wamid.1"))
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64

    response = client.post("/api/v1/webhook", content=raw, headers=headers)

    assert response.status_code == 403


def test_signed_message_is_accepted_and_answered(client: TestClient) -> None:
    raw, headers = _signed(_text_event("hi", "wamid.hello"))

    response = client.post("/api/v1/webhook", content=raw, headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "messages": 1,
        "ignored": 0,
        "throttled": 0,
        "stale": 0,
    }

    # TestClient runs background tasks before returning, so the reply is already out.
    outbox = client.get("/api/v1/simulate/outbox").json()
    assert outbox["enabled"] is True
    assert any(
        "How can I help you today?" in message["text"]
        for message in outbox["messages"]
    )


# --------------------------------------------------------------------------- #
# Development allowlist
# --------------------------------------------------------------------------- #
def test_message_from_a_stranger_is_dropped_silently(client: TestClient) -> None:
    """The whole point of the guard: a real customer must get no reaction."""
    raw, headers = _signed(_text_event("hi", "wamid.stranger", sender=STRANGER))

    response = client.post("/api/v1/webhook", content=raw, headers=headers)

    # Still a 200 - a 4xx or 5xx would have Meta retry the same message.
    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "messages": 0,
        "ignored": 1,
        "throttled": 0,
        "stale": 0,
    }

    # No reply went out...
    outbox = client.get("/api/v1/simulate/outbox").json()
    assert outbox["messages"] == []

    # ...and the drop happened early enough to leave no trace in the database:
    # no user row, so no conversation and no message history either.
    assert _user_rows(client, STRANGER) == 0


def test_outbound_guard_blocks_a_stranger_even_if_reached(client: TestClient) -> None:
    """Second layer: the send itself is refused, not just the inbound message."""
    container = client.app.state.container  # type: ignore[attr-defined]

    sent = asyncio.run(container.messaging.send(STRANGER, OutboundMessage(text="hi")))

    assert sent is None
    assert STRANGER in container.messaging.blocked

    outbox = client.get("/api/v1/simulate/outbox").json()
    assert not any(message["to"] == STRANGER for message in outbox["messages"])


def test_allowlist_endpoint_reports_the_posture(client: TestClient) -> None:
    payload = client.get("/api/v1/simulate/allowlist").json()

    assert payload["enabled"] is True
    assert payload["allowed"] == [PHONE]
    assert payload["blocks_everyone"] is False


def test_status_only_event_is_ignored(client: TestClient) -> None:
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {"statuses": [{"id": "x", "status": "read"}]},
                    }
                ]
            }
        ],
    }
    raw, headers = _signed(body)

    response = client.post("/api/v1/webhook", content=raw, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_malformed_body_is_acknowledged_not_retried(client: TestClient) -> None:
    """A 4xx would make Meta redeliver something we can never parse."""
    digest = hmac.new(APP_SECRET.encode(), b"not json", hashlib.sha256).hexdigest()

    response = client.post(
        "/api/v1/webhook",
        content=b"not json",
        headers={"X-Hub-Signature-256": f"sha256={digest}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


# --------------------------------------------------------------------------- #
# Simulator and leads
# --------------------------------------------------------------------------- #
def test_simulator_drives_a_full_lead(client: TestClient) -> None:
    def say(text: str = "", reply_id: str | None = None) -> dict[str, object]:
        response = client.post(
            "/api/v1/simulate",
            json={"phone": PHONE, "text": text, "reply_id": reply_id},
        )
        assert response.status_code == 200
        return response.json()

    assert say("hi")["state"] == "MAIN_MENU"
    assert say(reply_id=copy.MENU_COUNSELOR)["state"] == "ASK_NAME"
    assert say("Rahul Verma")["state"] == "ASK_CALLBACK_TIME"
    assert say("tomorrow 3pm")["state"] == "ASK_REMARKS"

    final = say("please discuss the fees")
    assert final["state"] == "CLOSED"
    assert final["lead_id"] is not None

    leads = client.get("/api/v1/leads").json()
    assert leads["count"] == 1
    lead = leads["items"][0]
    assert lead["name"] == "Rahul Verma"
    assert lead["type"] == "PRE_SALES"
    assert lead["status"] == "NEW"
    assert lead["preferred_time"] is not None

    single = client.get(f"/api/v1/leads/{lead['id']}").json()
    assert single["id"] == lead["id"]


def test_missing_lead_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/leads/9999").status_code == 404


def test_openapi_is_served_outside_production(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200


# --------------------------------------------------------------------------- #
# Uptime monitors send HEAD
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/", "/api/v1/health", "/api/v1/health/ready"])
def test_head_is_accepted_wherever_get_is(client: TestClient, path: str) -> None:
    """UptimeRobot and friends send HEAD by default to avoid a response body.

    FastAPI - unlike plain Starlette - does not add HEAD to a GET route, so
    every ping returned 405. The monitor reported the service down and the
    keep-alive that stops a free instance sleeping never landed.
    """
    assert client.head(path).status_code == 200
    assert client.get(path).status_code == 200


def test_unsupported_methods_are_still_refused(client: TestClient) -> None:
    """Widening to HEAD must not open the endpoint to anything else."""
    assert client.post("/api/v1/health").status_code == 405
    assert client.delete("/api/v1/health").status_code == 405


def test_typing_is_requested_only_for_messages_that_get_a_reply(
    client: TestClient,
) -> None:
    """Showing "typing…" for a message the bot then ignores would be a lie."""
    from app.domain.enums import MessageKind
    from app.domain.messaging import InboundMessage

    seen: list[tuple[str, bool]] = []

    class Recorder:
        async def send(self, to: str, message: object) -> None:
            return None

        async def mark_read(self, wa_message_id: str, *, typing: bool = False) -> None:
            seen.append((wa_message_id, typing))

        async def close(self) -> None:
            return None

    container = client.app.state.container  # type: ignore[attr-defined]
    container.messaging = Recorder()

    from app.api.v1.webhook import _process

    service = container.conversation_service()
    actionable = InboundMessage(
        wa_message_id="wamid.text", from_phone=PHONE,
        kind=MessageKind.TEXT, text="hi",
    )
    sticker = InboundMessage(
        wa_message_id="wamid.sticker", from_phone=PHONE,
        kind=MessageKind.UNSUPPORTED,
    )

    asyncio.run(_process(service, container, actionable))
    asyncio.run(_process(service, container, sticker))

    assert ("wamid.text", True) in seen
    assert ("wamid.sticker", False) in seen


# --------------------------------------------------------------------------- #
# Stale redelivered messages
# --------------------------------------------------------------------------- #
def _text_event_at(text: str, message_id: str, *, sent_at: float) -> dict[str, object]:
    """A webhook carrying an explicit WhatsApp send time."""
    event = _text_event(text, message_id)
    value = event["entry"][0]["changes"][0]["value"]  # type: ignore[index]
    value["messages"][0]["timestamp"] = str(int(sent_at))  # type: ignore[index]
    return event


def test_a_message_redelivered_hours_later_is_not_answered(
    client: TestClient,
) -> None:
    """Observed in production: a reply arrived at 09:56 to a message from
    00:46 the night before, with no user action in between.

    Meta retries webhooks it could not deliver, with backoff, for hours. A free
    instance that was asleep therefore wakes to a backlog - and answering it
    reaches the customer as the bot messaging them out of nowhere.
    """
    nine_hours_ago = time.time() - 9 * 3600
    raw, headers = _signed(
        _text_event_at("yes", "wamid.stale", sent_at=nine_hours_ago)
    )

    response = client.post("/api/v1/webhook", content=raw, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["stale"] == 1
    assert body["messages"] == 0

    outbox = client.get("/api/v1/simulate/outbox").json()
    assert outbox["messages"] == [], "a stale message must produce no reply"


def test_a_recent_message_is_answered_normally(client: TestClient) -> None:
    """The guard must not touch ordinary traffic, which arrives in seconds."""
    raw, headers = _signed(
        _text_event_at("hi", "wamid.fresh", sent_at=time.time() - 3)
    )

    response = client.post("/api/v1/webhook", content=raw, headers=headers)

    body = response.json()
    assert body["stale"] == 0
    assert body["messages"] == 1


def test_a_message_without_a_timestamp_is_still_answered(
    client: TestClient,
) -> None:
    """Fails open: dropping a real message is far worse than answering an old one."""
    raw, headers = _signed(_text_event("hi", "wamid.no-timestamp"))

    body = client.post("/api/v1/webhook", content=raw, headers=headers).json()

    assert body["stale"] == 0
    assert body["messages"] == 1
