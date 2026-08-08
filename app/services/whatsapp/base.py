"""Messaging channel abstraction."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.messaging import OutboundMessage


@runtime_checkable
class MessagingClient(Protocol):
    """Anything able to deliver a reply to a user.

    The bot depends on this, not on WhatsApp, which is what allows the test
    suite to run the whole conversation without a network call.
    """

    async def send(self, to: str, message: OutboundMessage) -> str | None:
        """Deliver one message. Returns the provider message id when available."""
        ...

    async def mark_read(self, wa_message_id: str, *, typing: bool = False) -> None:
        """Show the blue ticks, and optionally the "typing…" bubble.

        Best-effort; failures are swallowed. Both are cosmetic, and neither is
        worth failing a reply over.
        """
        ...

    async def close(self) -> None:
        """Release network resources."""
        ...
