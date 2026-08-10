"""Internal agent console: read every conversation, take one over, hand it back.

Kept apart from the public API on purpose. These endpoints return customer
phone numbers and full transcripts, so every one of them sits behind
`require_console_session`, and the whole router refuses to serve at all unless
the console is explicitly enabled and fully configured.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from app.api.deps import ContainerDep, SessionDep, SettingsDep
from app.core.console_auth import (
    SESSION_TTL_SECONDS,
    issue_session,
    read_session,
    verify_password,
)
from app.core.logging import get_logger
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.models.user import User
from app.domain.enums import MessageSender
from app.domain.messaging import OutboundMessage

logger = get_logger(__name__)

router = APIRouter(prefix="/console", tags=["console"])

#: Name of the session cookie.
COOKIE_NAME = "iscale_console"

#: WhatsApp only accepts a free-text message within 24 hours of the customer's
#: last inbound. Past that, Meta rejects the send and only a pre-approved
#: template gets through - so the console shows the window rather than letting
#: an agent type a reply that will silently fail.
SERVICE_WINDOW = timedelta(hours=24)


def _require_enabled(settings: SettingsDep) -> None:
    if not settings.console_ready:
        # 404 rather than 403: an unconfigured console should not advertise
        # that it exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


async def require_console_session(
    request: Request, settings: SettingsDep
) -> str:
    """Username from the session cookie, or 401."""
    _require_enabled(settings)
    token = request.cookies.get(COOKIE_NAME, "")
    username = read_session(token, settings.console_session_secret.get_secret_value())
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in"
        )
    return username


AgentDep = Annotated[str, Depends(require_console_session)]


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
_WEB_DIR = Path(__file__).resolve().parents[2] / "web"


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def login_page(request: Request, settings: SettingsDep) -> Response:
    """Login form, or straight through if the cookie is still valid."""
    _require_enabled(settings)
    token = request.cookies.get(COOKIE_NAME, "")
    if read_session(token, settings.console_session_secret.get_secret_value()):
        return RedirectResponse(url=f"{settings.api_prefix}/console/app", status_code=303)
    return _page("login.html")


@router.get("/app", include_in_schema=False)
async def inbox_page(request: Request, settings: SettingsDep) -> Response:
    """The inbox. Redirects rather than 401s - this is a browser page."""
    _require_enabled(settings)
    token = request.cookies.get(COOKIE_NAME, "")
    if not read_session(token, settings.console_session_secret.get_secret_value()):
        return RedirectResponse(url=f"{settings.api_prefix}/console", status_code=303)
    return _page("inbox.html")


def _page(name: str) -> Response:
    path = _WEB_DIR / name
    if not path.is_file():  # pragma: no cover - packaging error
        raise HTTPException(status_code=500, detail=f"Missing page: {name}")
    return HTMLResponse(
        path.read_text(encoding="utf-8"),
        headers={
            # The console lists customer phone numbers and transcripts. Keep it
            # out of caches and out of search engines.
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer",
        },
    )


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/api/login", summary="Sign in to the console")
async def login(
    payload: LoginRequest, response: Response, settings: SettingsDep
) -> dict[str, str]:
    _require_enabled(settings)

    # scrypt is deliberately slow (~1s, more on a small instance) and blocks the
    # thread it runs on. Off the event loop it would stall every WhatsApp
    # conversation in flight for the duration of one login.
    correct = await asyncio.to_thread(
        verify_password,
        payload.password,
        settings.console_password_hash.get_secret_value(),
    )
    # Compared after the hash either way: returning early on an unknown username
    # would make the two cases distinguishable by response time.
    if not correct or payload.username != settings.console_username:
        logger.warning(
            "Console login failed", extra={"username": payload.username[:40]}
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    token = issue_session(
        settings.console_username, settings.console_session_secret.get_secret_value()
    )
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,  # unreadable from JavaScript, so XSS cannot steal it
        samesite="lax",
        secure=settings.is_production,  # HTTPS-only once deployed
        path="/",
    )
    logger.info("Console login succeeded", extra={"username": settings.console_username})
    return {"status": "ok", "username": settings.console_username}


@router.post("/api/logout", summary="Sign out")
async def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/api/me", summary="Who am I")
async def me(agent: AgentDep) -> dict[str, str]:
    return {"username": agent}


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #
@router.get("/api/conversations", summary="Every number that has messaged us")
async def list_conversations(
    agent: AgentDep, session: SessionDep, limit: int = 200
) -> dict[str, Any]:
    """Most recently active first - the order an inbox is actually read in."""
    latest = (
        select(
            Conversation.user_id.label("user_id"),
            func.max(Conversation.last_activity_at).label("last_at"),
        )
        .group_by(Conversation.user_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(User, latest.c.last_at)
            .join(latest, latest.c.user_id == User.id)
            .order_by(desc(latest.c.last_at))
            .limit(min(limit, 500))
        )
    ).all()

    conversations = []
    for user, last_at in rows:
        preview = (
            await session.execute(
                select(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Conversation.user_id == user.id)
                .order_by(desc(Message.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        conversations.append(
            {
                "phone": user.phone,
                "name": user.name or user.profile_name or "",
                "last_activity": _iso(last_at),
                "last_message": (preview.message[:120] if preview else ""),
                "last_sender": str(preview.sender) if preview else "",
                "bot_paused": bool(user.bot_paused),
            }
        )
    return {"conversations": conversations}


@router.get("/api/messages/{phone}", summary="One thread")
async def get_messages(
    phone: str,
    agent: AgentDep,
    session: SessionDep,
    after_id: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    """The transcript, oldest first.

    `after_id` is what makes 2-second polling cheap: the page asks only for
    what it has not seen, so a long thread is transferred once and then only
    grows by whatever actually arrived.
    """
    user = await _get_user(session, phone)

    rows = (
        await session.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.user_id == user.id, Message.id > after_id)
            .order_by(Message.id)
            .limit(min(limit, 1000))
        )
    ).scalars().all()

    last_inbound = (
        await session.execute(
            select(func.max(Message.timestamp))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.user_id == user.id,
                Message.sender == MessageSender.USER,
            )
        )
    ).scalar_one_or_none()

    return {
        "phone": user.phone,
        "name": user.name or user.profile_name or "",
        "bot_paused": bool(user.bot_paused),
        "can_reply": _within_service_window(last_inbound),
        "window_expires": _iso(_window_end(last_inbound)),
        "messages": [
            {
                "id": m.id,
                "sender": str(m.sender),
                "text": m.message,
                "at": _iso(m.timestamp),
            }
            for m in rows
        ],
    }


# --------------------------------------------------------------------------- #
# Handover
# --------------------------------------------------------------------------- #
class HandoverRequest(BaseModel):
    phone: str
    paused: bool


@router.post("/api/handover", summary="Take a conversation over, or hand it back")
async def set_handover(
    payload: HandoverRequest, agent: AgentDep, session: SessionDep
) -> dict[str, Any]:
    user = await _get_user(session, payload.phone)
    user.bot_paused = payload.paused
    user.paused_at = datetime.now(UTC) if payload.paused else None
    await session.commit()

    logger.info(
        "Console handover changed",
        extra={"agent": agent, "paused": payload.paused, "phone": _mask(user.phone)},
    )
    return {"phone": user.phone, "bot_paused": user.bot_paused}


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
class SendRequest(BaseModel):
    phone: str
    text: str = Field(min_length=1, max_length=4096)


@router.post("/api/send", summary="Send a message as a human agent")
async def send_message(
    payload: SendRequest,
    agent: AgentDep,
    session: SessionDep,
    container: ContainerDep,
) -> dict[str, Any]:
    """Send, then record. Recorded only on success, so the transcript never
    shows a message the customer did not receive.
    """
    user = await _get_user(session, payload.phone)

    if not user.bot_paused:
        # Refusing is kinder than allowing it: the bot would answer the same
        # customer a second later, and the two replies would contradict.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Take the conversation over from the bot before sending.",
        )

    conversation = (
        await session.execute(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(desc(Conversation.last_activity_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No conversation found"
        )

    try:
        await container.messaging.send(user.phone, OutboundMessage(text=payload.text))
    except Exception as exc:
        logger.exception("Agent message failed to send", extra={"agent": agent})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "WhatsApp rejected the message. If the customer has not written "
                "in the last 24 hours, only a template can reach them."
            ),
        ) from exc

    message = Message(
        conversation_id=conversation.id,
        sender=MessageSender.AGENT,
        message=payload.text,
        state=conversation.current_state,
        timestamp=datetime.now(UTC),
    )
    session.add(message)
    conversation.last_activity_at = datetime.now(UTC)
    await session.commit()

    logger.info("Agent message sent", extra={"agent": agent, "phone": _mask(user.phone)})
    return {"id": message.id, "sender": str(MessageSender.AGENT), "text": payload.text}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _get_user(session: SessionDep, phone: str) -> User:
    user = (
        await session.execute(select(User).where(User.phone == phone))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown number"
        )
    return user


def _window_end(last_inbound: datetime | None) -> datetime | None:
    if last_inbound is None:
        return None
    if last_inbound.tzinfo is None:
        last_inbound = last_inbound.replace(tzinfo=UTC)
    return last_inbound + SERVICE_WINDOW


def _within_service_window(last_inbound: datetime | None) -> bool:
    end = _window_end(last_inbound)
    return end is not None and end > datetime.now(UTC)


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _mask(phone: str) -> str:
    return f"{phone[:4]}***{phone[-3:]}" if len(phone) > 7 else "***"
