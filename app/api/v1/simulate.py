"""Local conversation simulator.

Drives the real state machine, repositories and LLM call without WhatsApp, so
the whole flow can be exercised with curl while developing. Disabled in
production because it lets a caller act as any phone number.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, ConversationServiceDep
from app.container import Container
from app.domain.enums import MessageKind
from app.domain.messaging import InboundMessage
from app.services.whatsapp.base import MessagingClient
from app.services.whatsapp.guarded_client import GuardedMessagingClient
from app.services.whatsapp.logging_client import LoggingMessagingClient

router = APIRouter(prefix="/simulate", tags=["simulate"])


class SimulateRequest(BaseModel):
    phone: str = Field(default="919999999999", description="Sender's number, digits only")
    text: str = Field(default="hi", description="What the user typed")
    reply_id: str | None = Field(
        default=None, description="Option id, as if a button or list row was tapped"
    )
    profile_name: str | None = None


class SimulatedReply(BaseModel):
    text: str
    options: list[dict[str, str]] = Field(default_factory=list)


class SimulateResponse(BaseModel):
    state: str
    replies: list[SimulatedReply]
    lead_id: int | None = None


@router.post("", response_model=SimulateResponse, summary="Send a message as a test user")
async def simulate(
    payload: SimulateRequest,
    container: ContainerDep,
    conversation_service: ConversationServiceDep,
) -> SimulateResponse:
    if container.settings.is_production:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not available"
        )

    inbound = InboundMessage(
        wa_message_id=f"sim-{uuid.uuid4().hex}",
        from_phone=payload.phone,
        kind=MessageKind.INTERACTIVE if payload.reply_id else MessageKind.TEXT,
        text=payload.text,
        reply_id=payload.reply_id,
        profile_name=payload.profile_name,
    )

    result = await conversation_service.process_inbound(inbound)

    state = await _current_state(container, payload.phone)
    return SimulateResponse(
        state=state,
        replies=[
            SimulatedReply(
                text=reply.text,
                options=[{"id": oid, "title": title} for oid, title in reply.options],
            )
            for reply in result.replies
        ],
        lead_id=result.lead_id,
    )


@router.get("/outbox", summary="Messages the logging client would have sent")
async def outbox(container: ContainerDep) -> dict[str, object]:
    """Only populated when `WHATSAPP_ENABLED=false`."""
    messaging = _unwrap(container.messaging)
    if not isinstance(messaging, LoggingMessagingClient):
        return {"enabled": False, "messages": []}
    return {
        "enabled": True,
        "messages": [
            {"to": to, "text": message.text, "options": list(message.options)}
            for to, message in messaging.sent
        ],
    }


@router.get("/allowlist", summary="Who the bot is currently allowed to reply to")
async def allowlist_status(container: ContainerDep) -> dict[str, object]:
    """Confirms the development guard is on before pointing at a live number."""
    guard = container.allowlist
    outer = container.messaging
    return {
        "enabled": guard.enabled,
        "status": guard.describe(),
        "allowed": sorted(guard.numbers),
        "blocks_everyone": guard.blocks_everyone,
        "blocked_recipients": (
            list(outer.blocked) if isinstance(outer, GuardedMessagingClient) else []
        ),
    }


def _unwrap(messaging: MessagingClient) -> MessagingClient:
    """See through the allowlist guard to the client it decorates."""
    while isinstance(messaging, GuardedMessagingClient):
        messaging = messaging.inner
    return messaging


async def _current_state(container: Container, phone: str) -> str:
    from app.repositories.conversation_repository import ConversationRepository
    from app.repositories.user_repository import UserRepository

    async with container.database.session() as session:
        user = await UserRepository(session).get_by_phone(phone)
        if user is None:
            return "UNKNOWN"
        conversation = await ConversationRepository(session).get_active(user.id)
        return str(conversation.current_state) if conversation else "CLOSED"
