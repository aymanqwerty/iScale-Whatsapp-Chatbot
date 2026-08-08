"""WhatsApp Cloud API webhook.

Two contracts Meta imposes, both of which shape this module:

* **Verification** - a GET with `hub.challenge` that must echo the challenge as
  plain text when the verify token matches.
* **Fast 200** - the POST must be acknowledged quickly. Meta retries anything
  slow or non-200, which would deliver the same message repeatedly. So the
  payload is parsed, acknowledged, and processed in a background task.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request, Response, status

from app.api.deps import ContainerDep, ConversationServiceDep
from app.container import Container
from app.core.logging import correlation_id_var, get_logger
from app.domain.messaging import InboundMessage
from app.schemas.whatsapp import WebhookPayload
from app.services.conversation_service import ConversationService
from app.services.whatsapp.parser import parse_webhook, verify_signature

logger = get_logger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


@router.get("", summary="Webhook verification handshake")
async def verify(
    container: ContainerDep,
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
) -> Response:
    expected = container.settings.whatsapp_verify_token.get_secret_value()

    if hub_mode == "subscribe" and hub_verify_token and hub_verify_token == expected:
        logger.info("Webhook verification succeeded")
        # Meta requires the raw challenge string, not JSON.
        return Response(content=hub_challenge or "", media_type="text/plain")

    logger.warning("Webhook verification failed", extra={"mode": hub_mode})
    return Response(status_code=status.HTTP_403_FORBIDDEN, content="Verification failed")


@router.post("", summary="Receive WhatsApp events", status_code=status.HTTP_200_OK)
async def receive(
    request: Request,
    background_tasks: BackgroundTasks,
    container: ContainerDep,
    conversation_service: ConversationServiceDep,
    x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> Any:
    settings = container.settings

    # Shed obvious floods before reading the body or computing an HMAC. Meta
    # bursts legitimately on redelivery, so the ceiling is generous - this is
    # here to stop a flood, not to police normal traffic.
    if container.webhook_limiter is not None:
        source = request.client.host if request.client else "unknown"
        if not container.webhook_limiter.allow(source):
            logger.warning("Webhook rate limit exceeded", extra={"source": source})
            return Response(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content="Too many requests",
            )

    raw_body = await request.body()

    app_secret = settings.whatsapp_app_secret.get_secret_value()
    if app_secret:
        if not verify_signature(raw_body, x_hub_signature_256, app_secret):
            logger.warning("Rejected webhook with an invalid signature")
            # 403, not 401: Meta stops retrying on a 4xx, which is what we want
            # for a request that was never legitimately ours.
            return Response(
                status_code=status.HTTP_403_FORBIDDEN, content="Invalid signature"
            )
    elif settings.is_production:
        # Refusing to run unauthenticated in production is safer than a warning
        # nobody reads.
        logger.error("WHATSAPP_APP_SECRET is not set - refusing unverified webhook")
        return Response(
            status_code=status.HTTP_403_FORBIDDEN,
            content="Signature verification unavailable",
        )
    else:
        logger.warning("Webhook signature not verified (no app secret configured)")

    try:
        payload = WebhookPayload.model_validate_json(raw_body)
    except ValueError:
        # Malformed payloads are acknowledged, not retried - a 4xx here would
        # have Meta redeliver something we can never parse.
        logger.warning("Could not parse webhook payload")
        return {"status": "ignored"}

    messages = parse_webhook(payload)
    if not messages:
        return {"status": "ignored"}

    # The inbound half of the development guard. Dropping here - before any
    # database write, LLM call or read receipt - means a message from a real
    # customer leaves no trace and gets no reaction of any kind. The number is
    # still acknowledged with a 200 so Meta does not retry it.
    allowlist = container.allowlist
    accepted = [m for m in messages if allowlist.allows(m.from_phone)]
    ignored = len(messages) - len(accepted)
    if ignored:
        logger.info(
            "Dropped inbound message(s) from non-allowlisted number(s)",
            extra={"dropped": ignored},
        )

    # Per-sender ceiling. Every message that gets past here costs an LLM call,
    # so one person sending in a loop is a bill as well as a queue. Throttled
    # messages are dropped silently rather than answered with "slow down":
    # someone hammering send is not reading, and a reply would double the
    # traffic we are trying to reduce.
    if container.sender_limiter is not None:
        within_budget = []
        for inbound in accepted:
            if container.sender_limiter.allow(inbound.from_phone):
                within_budget.append(inbound)
            else:
                logger.warning(
                    "Sender rate limit exceeded - dropping message",
                    extra={"phone": _mask(inbound.from_phone)},
                )
        throttled = len(accepted) - len(within_budget)
        accepted = within_budget
    else:
        throttled = 0

    for inbound in accepted:
        background_tasks.add_task(
            _process, conversation_service, container, inbound
        )

    logger.info("Webhook accepted", extra={"messages": len(accepted)})
    return {
        "status": "accepted",
        "messages": len(accepted),
        "ignored": ignored,
        "throttled": throttled,
    }


def _mask(phone: str) -> str:
    return f"{phone[:4]}***{phone[-3:]}" if len(phone) > 7 else "***"


async def _process(
    service: ConversationService, container: Container, inbound: InboundMessage
) -> None:
    """Background worker for one message, with its own correlation id.

    The read receipt goes out first, carrying the typing indicator with it, so
    the user sees "typing…" while retrieval and the model call happen. Meta
    clears the bubble when the reply lands or after ~25 seconds, so there is
    nothing to turn off - and a failure mid-turn cannot leave it stuck on.
    """
    token = correlation_id_var.set(uuid.uuid4().hex[:12])
    try:
        # Only for messages that will actually produce a reply. Showing
        # "typing…" for a message the bot then ignores would be a lie.
        await container.messaging.mark_read(
            inbound.wa_message_id, typing=inbound.is_actionable
        )
        await service.process_inbound(inbound)
    finally:
        correlation_id_var.reset(token)
