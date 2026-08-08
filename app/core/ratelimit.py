"""In-memory sliding-window rate limiting.

Two things need bounding, for different reasons:

* **Per sender.** Every processed message costs a Groq call. One person holding
  down send - or a loop in someone else's integration - turns into real money
  and a stalled queue for everyone else.
* **Per source address.** Unsigned webhook floods are already cheap to reject
  (an HMAC compare), but they still occupy workers.

Deliberately in-process and dependency-free. That is the honest trade: it is
exact for a single instance and approximate across several, since each replica
counts only what it sees. Running more than one replica means moving this to
Redis - the `RateLimiter` interface is the seam for that.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from app.core.logging import get_logger

logger = get_logger(__name__)


class SlidingWindowLimiter:
    """Allows `limit` events per `window_seconds` for each key."""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        max_keys: int = 20_000,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._window = float(window_seconds)
        self._max_keys = max_keys
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    # ------------------------------------------------------------------ #
    def allow(self, key: str) -> bool:
        """Record an event for `key` and report whether it is within budget."""
        now = time.monotonic()
        window_start = now - self._window

        hits = self._hits[key]
        while hits and hits[0] < window_start:
            hits.popleft()

        if len(hits) >= self._limit:
            return False

        hits.append(now)
        if len(self._hits) > self._max_keys:
            self._evict(window_start)
        return True

    def remaining(self, key: str) -> int:
        """Budget left for `key`, without consuming any of it."""
        window_start = time.monotonic() - self._window
        hits = self._hits.get(key)
        if not hits:
            return self._limit
        live = sum(1 for stamp in hits if stamp >= window_start)
        return max(0, self._limit - live)

    def _evict(self, window_start: float) -> None:
        """Drop keys with nothing left in the window.

        Without this an attacker sending from many spoofed identifiers would
        grow the dictionary without bound - the rate limiter becoming the
        memory leak it was added to prevent.
        """
        stale = [
            key
            for key, hits in self._hits.items()
            if not hits or hits[-1] < window_start
        ]
        for key in stale:
            del self._hits[key]
        if not stale:
            # Everything is live: keep the newest keys and drop the rest rather
            # than grow without limit.
            ordered = sorted(self._hits.items(), key=lambda kv: kv[1][-1], reverse=True)
            for key, _ in ordered[self._max_keys :]:
                del self._hits[key]
        logger.debug("Rate limiter evicted keys", extra={"remaining": len(self._hits)})

    def __len__(self) -> int:
        return len(self._hits)
