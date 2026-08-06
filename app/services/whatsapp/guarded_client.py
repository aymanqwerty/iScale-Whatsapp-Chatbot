"""Outbound allowlist guard.

Decorates any `MessagingClient` and drops sends to numbers that are not
allowlisted. This is the last line of defence: the webhook already filters
inbound traffic, so reaching here with a blocked number means something else
went wrong - which is exactly when the guard earns its place.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.domain.messaging import OutboundMessage
from app.services.whatsapp.allowlist import PhoneAllowlist
from app.services.whatsapp.base import MessagingClient

logger = get_logger(__name__)


class GuardedMessagingClient:
    """A `MessagingClient` that refuses to message non-allowlisted numbers."""

    def __init__(self, inner: MessagingClient, allowlist: PhoneAllowlist) -> None:
        self._inner = inner
        self._allowlist = allowlist
        #: Recipients that were blocked, for the dev-mode diagnostics endpoint.
        self.blocked: list[str] = []

    @property
    def inner(self) -> MessagingClient:
        return self._inner

    async def send(self, to: str, message: OutboundMessage) -> str | None:
        if not self._allowlist.allows(to):
            self.blocked.append(to)
            logger.warning(
                "BLOCKED outbound message - recipient is not allowlisted",
                extra={"to": _mask(to), "preview": message.text[:60]},
            )
            return None
        return await self._inner.send(to, message)

    async def mark_read(self, wa_message_id: str) -> None:
        # Read receipts are only ever sent for messages we chose to process,
        # and those already passed the inbound filter.
        await self._inner.mark_read(wa_message_id)

    async def close(self) -> None:
        await self._inner.close()


def _mask(phone: str) -> str:
    return f"{phone[:4]}***{phone[-3:]}" if len(phone) > 7 else "***"
