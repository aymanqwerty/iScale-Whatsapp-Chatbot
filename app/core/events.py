"""In-process fan-out for console live updates.

Deliberately tiny and deliberately in-memory. The whole app runs as a single
Render instance, so the bot's background task, the console's send endpoint and
every open WebSocket share one process - a Redis hop would add an external
dependency to solve a problem that does not exist yet.

If this ever runs on more than one instance, this is the piece to replace: swap
`MessageBroadcaster` for a Redis pub/sub with the same two methods, and nothing
else changes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Per-subscriber buffer. A console tab that stops reading (laptop asleep,
#: network stalled) must not grow a queue without bound, and it does not need
#: the backlog either - every event is just "something changed, go and look",
#: so dropping the oldest costs nothing.
_QUEUE_SIZE = 64


class MessageBroadcaster:
    """Publishes "this phone number has a new message" to every listener."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[str]]:
        """Register a listener for the life of the block."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def publish(self, phone: str) -> None:
        """Signal that `phone` has new activity.

        Never awaits and never raises: this is called from the middle of a
        WhatsApp turn, and a stalled console tab must not be able to slow down
        or break a customer conversation.
        """
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(phone)
            except asyncio.QueueFull:
                # The listener is not keeping up. Drop the oldest and retry
                # once - a missed signal would leave a thread looking stale
                # until the fallback poll catches it.
                try:
                    queue.get_nowait()
                    queue.put_nowait(phone)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    pass

    @property
    def listener_count(self) -> int:
        return len(self._subscribers)


#: Process-wide instance. Imported directly rather than injected because both
#: publishers sit deep inside request handling, and threading it through every
#: layer would add plumbing to a dozen signatures for no testability gain -
#: the class itself is trivially testable on its own.
broadcaster = MessageBroadcaster()
