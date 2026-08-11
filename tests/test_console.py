"""The internal agent console: auth, listing, handover and agent sending."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.core.config import Settings
from app.core.console_auth import hash_password
from app.db.base import Base
from app.main import create_app
from tests.conftest import PROJECT_ROOT, FakeLLM, FrozenClockValidator

USER_PHONE = "919876543210"
PASSWORD = "console-test-pass"
BASE = "/api/v1/console"


def _settings(db_file: Path, **overrides: object) -> Settings:
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
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = _settings(
        tmp_path / "console.db",
        console_enabled=True,
        console_username="iScale-user",
        console_password_hash=hash_password(PASSWORD),
        console_session_secret="test-signing-secret",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        container = app.state.container
        container.answer_service._llm = FakeLLM(reply="A grounded answer.")
        container.callback_validator = FrozenClockValidator(settings)
        yield test_client


def _login(client: TestClient) -> None:
    response = client.post(
        f"{BASE}/api/login",
        json={"username": "iScale-user", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _talk(client: TestClient, text: str = "hi") -> None:
    """Drive one real message through the bot via the simulator."""
    response = client.post("/api/v1/simulate", json={"phone": USER_PHONE, "text": text})
    assert response.status_code == 200, response.text


def _outbox(client: TestClient) -> list[dict[str, str]]:
    return list(client.get("/api/v1/simulate/outbox").json()["messages"])


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def test_everything_requires_a_session(client: TestClient) -> None:
    """No cookie, no customer data - on every endpoint, not just the page."""
    for path in ("/api/conversations", f"/api/messages/{USER_PHONE}", "/api/me"):
        assert client.get(BASE + path).status_code == 401, path

    assert (
        client.post(
            f"{BASE}/api/handover", json={"phone": USER_PHONE, "paused": True}
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"{BASE}/api/send", json={"phone": USER_PHONE, "text": "x"}
        ).status_code
        == 401
    )


def test_wrong_password_is_rejected(client: TestClient) -> None:
    response = client.post(
        f"{BASE}/api/login", json={"username": "iScale-user", "password": "nope"}
    )

    assert response.status_code == 401
    assert client.get(f"{BASE}/api/conversations").status_code == 401


def test_wrong_username_is_rejected(client: TestClient) -> None:
    response = client.post(
        f"{BASE}/api/login", json={"username": "someone-else", "password": PASSWORD}
    )

    assert response.status_code == 401


def test_a_forged_cookie_is_rejected(client: TestClient) -> None:
    """The HMAC signature is the whole security boundary."""
    client.cookies.set(
        "iscale_console",
        "eyJ1IjoiaVNjYWxlLXVzZXIiLCJleHAiOjk5OTk5OTk5OTl9.not-a-real-signature",
    )

    assert client.get(f"{BASE}/api/conversations").status_code == 401


def test_login_then_logout(client: TestClient) -> None:
    _login(client)
    assert client.get(f"{BASE}/api/me").json()["username"] == "iScale-user"

    client.post(f"{BASE}/api/logout")

    assert client.get(f"{BASE}/api/me").status_code == 401


def test_console_is_invisible_when_disabled(tmp_path: Path) -> None:
    """Unconfigured means 404, not a login page announcing it exists."""
    settings = _settings(tmp_path / "off.db", console_enabled=False)

    with TestClient(create_app(settings)) as client:
        assert client.get(BASE).status_code == 404
        assert (
            client.post(
                f"{BASE}/api/login", json={"username": "a", "password": "b"}
            ).status_code
            == 404
        )


def test_a_half_configured_console_stays_off(tmp_path: Path) -> None:
    """Enabled with no signing secret would accept any forged cookie."""
    settings = _settings(
        tmp_path / "half.db",
        console_enabled=True,
        console_password_hash=hash_password("x"),
        console_session_secret="",
    )

    with TestClient(create_app(settings)) as client:
        assert client.get(BASE).status_code == 404


# --------------------------------------------------------------------------- #
# Reading conversations
# --------------------------------------------------------------------------- #
def test_conversations_and_thread_are_visible(client: TestClient) -> None:
    _talk(client, "hi")
    _login(client)

    convos = client.get(f"{BASE}/api/conversations").json()["conversations"]
    assert any(c["phone"] == USER_PHONE for c in convos)

    thread = client.get(f"{BASE}/api/messages/{USER_PHONE}").json()
    senders = {m["sender"] for m in thread["messages"]}
    assert "USER" in senders and "BOT" in senders
    assert thread["bot_paused"] is False


def test_polling_only_returns_what_is_new(client: TestClient) -> None:
    """`after_id` is what makes 2-second polling cheap."""
    _talk(client, "hi")
    _login(client)

    first = client.get(f"{BASE}/api/messages/{USER_PHONE}").json()["messages"]
    highest = max(m["id"] for m in first)

    unchanged = client.get(
        f"{BASE}/api/messages/{USER_PHONE}?after_id={highest}"
    ).json()["messages"]
    assert unchanged == []

    _talk(client, "what courses do you have")

    fresh = client.get(f"{BASE}/api/messages/{USER_PHONE}?after_id={highest}").json()[
        "messages"
    ]
    assert fresh and all(m["id"] > highest for m in fresh)


def test_unknown_number_is_a_404(client: TestClient) -> None:
    _login(client)

    assert client.get(f"{BASE}/api/messages/910000000000").status_code == 404


# --------------------------------------------------------------------------- #
# Handover
# --------------------------------------------------------------------------- #
def test_handover_silences_the_bot_but_keeps_recording(client: TestClient) -> None:
    """The whole point: messages still arrive, no reply goes out."""
    _talk(client, "hi")
    _login(client)
    client.post(f"{BASE}/api/handover", json={"phone": USER_PHONE, "paused": True})

    before = len(client.get(f"{BASE}/api/messages/{USER_PHONE}").json()["messages"])
    sent_before = len(_outbox(client))

    _talk(client, "is anyone there")

    thread = client.get(f"{BASE}/api/messages/{USER_PHONE}").json()
    assert len(thread["messages"]) == before + 1, "the message was not recorded"
    assert thread["messages"][-1]["text"] == "is anyone there"
    assert len(_outbox(client)) == sent_before, "the bot replied during a handover"


def test_handing_back_resumes_the_bot(client: TestClient) -> None:
    _talk(client, "hi")
    _login(client)
    client.post(f"{BASE}/api/handover", json={"phone": USER_PHONE, "paused": True})
    _talk(client, "hello?")

    client.post(f"{BASE}/api/handover", json={"phone": USER_PHONE, "paused": False})
    before = len(_outbox(client))
    _talk(client, "hi")

    assert len(_outbox(client)) > before


def test_the_bot_sees_what_the_agent_said(client: TestClient) -> None:
    """Handing back must not lose the human half of the conversation."""
    _talk(client, "hi")
    _login(client)
    client.post(f"{BASE}/api/handover", json={"phone": USER_PHONE, "paused": True})
    client.post(
        f"{BASE}/api/send",
        json={"phone": USER_PHONE, "text": "Meera here, happy to help."},
    )
    client.post(f"{BASE}/api/handover", json={"phone": USER_PHONE, "paused": False})

    _talk(client, "what are the fees")

    calls = client.app.state.container.answer_service._llm.calls  # type: ignore[attr-defined]
    history = str(calls[-1].get("history", ""))
    assert "Meera here" in history, "the agent's message never reached the model"


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def test_sending_requires_taking_over_first(client: TestClient) -> None:
    """Otherwise the bot answers a second later and contradicts the human."""
    _talk(client, "hi")
    _login(client)

    response = client.post(
        f"{BASE}/api/send", json={"phone": USER_PHONE, "text": "hello"}
    )

    assert response.status_code == 409
    assert "take the conversation over" in response.json()["detail"].lower()


def test_an_agent_message_is_delivered_and_recorded(client: TestClient) -> None:
    _talk(client, "hi")
    _login(client)
    client.post(f"{BASE}/api/handover", json={"phone": USER_PHONE, "paused": True})

    response = client.post(
        f"{BASE}/api/send", json={"phone": USER_PHONE, "text": "Hi, Meera here."}
    )

    assert response.status_code == 200
    assert response.json()["sender"] == "AGENT"

    thread = client.get(f"{BASE}/api/messages/{USER_PHONE}").json()["messages"]
    assert thread[-1]["sender"] == "AGENT"
    assert thread[-1]["text"] == "Hi, Meera here."
    assert _outbox(client)[-1]["text"] == "Hi, Meera here."


def test_an_empty_message_is_rejected(client: TestClient) -> None:
    _talk(client, "hi")
    _login(client)
    client.post(f"{BASE}/api/handover", json={"phone": USER_PHONE, "paused": True})

    response = client.post(f"{BASE}/api/send", json={"phone": USER_PHONE, "text": ""})

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Live updates
# --------------------------------------------------------------------------- #
def test_the_stream_requires_a_session(client: TestClient) -> None:
    """The socket must not become a second, unauthenticated way in."""
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    with pytest.raises(WSDisconnect):
        with client.websocket_connect(f"{BASE}/api/stream"):
            pass


def test_the_stream_signals_new_activity(client: TestClient) -> None:
    """A nudge carrying only the number - never the message content."""
    _login(client)

    with client.websocket_connect(f"{BASE}/api/stream") as ws:
        _talk(client, "hi")
        event = ws.receive_json()

    assert event["type"] == "activity"
    assert event["phone"] == USER_PHONE
    assert "text" not in event and "message" not in event


def test_the_stream_signals_during_a_handover(client: TestClient) -> None:
    """The console is the only thing answering, so it must hear every message."""
    _talk(client, "hi")
    _login(client)
    client.post(f"{BASE}/api/handover", json={"phone": USER_PHONE, "paused": True})

    with client.websocket_connect(f"{BASE}/api/stream") as ws:
        _talk(client, "anyone there?")
        event = ws.receive_json()

    assert event["type"] == "activity"
    assert event["phone"] == USER_PHONE


def test_listing_conversations_is_one_query(client: TestClient) -> None:
    """Regression: a per-conversation preview query made the endpoint slow
    enough that browser polls overlapped and duplicated messages on screen.
    """
    from sqlalchemy import event as sa_event

    for index in range(6):
        phone = f"91999900{index:04d}"
        assert (
            client.post("/api/v1/simulate", json={"phone": phone, "text": "hi"}).status_code
            == 200
        )
    _login(client)

    engine = client.app.state.container.database.engine.sync_engine  # type: ignore[attr-defined]
    statements: list[str] = []

    def record(conn, cursor, statement, *args):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", record)
    try:
        payload = client.get(f"{BASE}/api/conversations").json()
    finally:
        sa_event.remove(engine, "before_cursor_execute", record)

    assert len(payload["conversations"]) >= 6
    assert len(statements) == 1, (
        f"expected one SELECT, got {len(statements)} - the N+1 is back"
    )


def test_the_list_carries_both_name_and_number(client: TestClient) -> None:
    _talk(client, "hi")
    _login(client)

    row = next(
        c
        for c in client.get(f"{BASE}/api/conversations").json()["conversations"]
        if c["phone"] == USER_PHONE
    )

    assert row["phone"] == USER_PHONE
    assert "name" in row
    assert row["last_message"]


# --------------------------------------------------------------------------- #
# Cross-origin auth for the separately hosted React console
# --------------------------------------------------------------------------- #
@pytest.fixture
def cors_client(tmp_path: Path) -> Iterator[TestClient]:
    """A backend configured for a frontend on another origin."""
    settings = _settings(
        tmp_path / "cors.db",
        console_enabled=True,
        console_username="iScale-user",
        console_password_hash=hash_password(PASSWORD),
        console_session_secret="test-signing-secret",
        console_allowed_origins="https://console.example.com,http://localhost:5173",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        container = app.state.container
        container.answer_service._llm = FakeLLM(reply="A grounded answer.")
        container.callback_validator = FrozenClockValidator(settings)
        yield test_client


def test_login_returns_a_token_for_the_hosted_frontend(client: TestClient) -> None:
    """Safari and Brave block third-party cookies, so the SPA needs the token."""
    response = client.post(
        f"{BASE}/api/login", json={"username": "iScale-user", "password": PASSWORD}
    )

    body = response.json()
    assert body["token"], "no token for a cross-origin client to store"
    assert body["expires_in"] > 0


def test_a_bearer_token_authenticates_without_any_cookie(client: TestClient) -> None:
    token = client.post(
        f"{BASE}/api/login", json={"username": "iScale-user", "password": PASSWORD}
    ).json()["token"]
    client.cookies.clear()

    response = client.get(
        f"{BASE}/api/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["username"] == "iScale-user"


def test_a_forged_bearer_token_is_rejected(client: TestClient) -> None:
    client.cookies.clear()

    response = client.get(
        f"{BASE}/api/me",
        headers={"Authorization": "Bearer eyJ1IjoiaVNjYWxlLXVzZXIifQ.forged"},
    )

    assert response.status_code == 401


def test_the_websocket_accepts_a_token_query_parameter(client: TestClient) -> None:
    """A browser cannot set an Authorization header on a WS handshake."""
    token = client.post(
        f"{BASE}/api/login", json={"username": "iScale-user", "password": PASSWORD}
    ).json()["token"]
    client.cookies.clear()

    with client.websocket_connect(f"{BASE}/api/stream?token={token}") as ws:
        _talk(client, "hi")
        assert ws.receive_json()["type"] == "activity"


def test_the_websocket_rejects_a_bad_token(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    client.cookies.clear()
    with pytest.raises(WSDisconnect):
        with client.websocket_connect(f"{BASE}/api/stream?token=nonsense"):
            pass


def test_an_allowed_origin_gets_cors_headers(cors_client: TestClient) -> None:
    response = cors_client.options(
        f"{BASE}/api/conversations",
        headers={
            "Origin": "https://console.example.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code in (200, 204)
    assert response.headers["access-control-allow-origin"] == "https://console.example.com"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_an_unknown_origin_is_not_allowed(cors_client: TestClient) -> None:
    """These endpoints return every customer transcript we hold."""
    response = cors_client.get(
        f"{BASE}/api/conversations", headers={"Origin": "https://evil.example.com"}
    )

    assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_cors_is_absent_when_no_origin_is_configured(client: TestClient) -> None:
    """A same-origin deployment must gain no new surface."""
    response = client.get(
        f"{BASE}/api/conversations", headers={"Origin": "https://anything.example.com"}
    )

    assert "access-control-allow-origin" not in response.headers


def test_the_cookie_relaxes_only_when_a_frontend_is_hosted_elsewhere(
    client: TestClient, cors_client: TestClient
) -> None:
    """SameSite=None is required cross-origin and needlessly loose otherwise."""
    same = client.post(
        f"{BASE}/api/login", json={"username": "iScale-user", "password": PASSWORD}
    )
    cross = cors_client.post(
        f"{BASE}/api/login", json={"username": "iScale-user", "password": PASSWORD}
    )

    assert "samesite=lax" in same.headers["set-cookie"].lower()
    cross_cookie = cross.headers["set-cookie"].lower()
    assert "samesite=none" in cross_cookie
    assert "secure" in cross_cookie, "SameSite=None is ignored without Secure"


def test_a_bare_host_is_upgraded_to_https(tmp_path: Path) -> None:
    """Observed in production: CONSOLE_ALLOWED_ORIGINS set without a scheme.

    Browsers always send the scheme in `Origin`, so a bare host matches nothing
    and every request is blocked before it reaches the server - meaning the
    logs show absolutely nothing to debug from.
    """
    settings = _settings(
        tmp_path / "bare.db",
        console_enabled=True,
        console_username="iScale-user",
        console_password_hash=hash_password(PASSWORD),
        console_session_secret="s",
        console_allowed_origins="console.vercel.app, https://other.example.com/",
    )

    assert settings.console_origins == [
        "https://console.vercel.app",
        "https://other.example.com",
    ]


def test_preview_deployments_can_be_matched_by_pattern(tmp_path: Path) -> None:
    """Vercel renames every deployment, so an exact URL breaks on the next push."""
    settings = _settings(
        tmp_path / "regex.db",
        console_enabled=True,
        console_username="iScale-user",
        console_password_hash=hash_password(PASSWORD),
        console_session_secret="s",
        console_allowed_origin_regex=r"^https://my-console-[a-z0-9-]+\.vercel\.app$",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        allowed = client.options(
            f"{BASE}/api/login",
            headers={
                "Origin": "https://my-console-abc123-team.vercel.app",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert (
            allowed.headers.get("access-control-allow-origin")
            == "https://my-console-abc123-team.vercel.app"
        )

        # Anchored to the project prefix: a bare `.*\.vercel\.app` would let any
        # Vercel project in the world call this API with credentials.
        stranger = client.options(
            f"{BASE}/api/login",
            headers={
                "Origin": "https://someone-else.vercel.app",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert (
            stranger.headers.get("access-control-allow-origin")
            != "https://someone-else.vercel.app"
        )
