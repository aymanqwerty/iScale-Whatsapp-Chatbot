"""Development allowlist.

While the bot is pointed at a live business number, its webhook receives *every*
message that number gets: real enquiries, and traffic belonging to other systems
sharing the same WhatsApp Business Account. Replying to any of it would be
visible to a real person and unrecoverable.

The allowlist is the guard. It is applied in two independent places:

* **Inbound** (`app/api/v1/webhook.py`) - a message from an unlisted number is
  dropped before anything happens: no database write, no LLM call, no read
  receipt, no reply.
* **Outbound** (`GuardedMessagingClient`) - a send to an unlisted number is
  refused even if some code path reaches that far.

One layer would be enough for the normal path; two mean a bug in the first
cannot put a message in front of a stranger.

Defaults are fail-closed: enabled with an empty list blocks everyone.
"""

from __future__ import annotations

import re

from app.core.logging import get_logger

logger = get_logger(__name__)

_NON_DIGITS = re.compile(r"\D")


def normalize_phone(raw: str) -> str:
    """Reduce a number to bare digits for comparison.

    WhatsApp reports `919876543210`, while people write `+91 98765-43210`.
    Comparing anything other than digits invites a mismatch that silently
    disables the guard.
    """
    return _NON_DIGITS.sub("", raw or "")


class PhoneAllowlist:
    """Decides which numbers the bot may talk to."""

    def __init__(self, *, enabled: bool, numbers: str) -> None:
        self._enabled = enabled
        self._numbers: frozenset[str] = frozenset(
            normalized
            for entry in numbers.split(",")
            if (normalized := normalize_phone(entry))
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def numbers(self) -> frozenset[str]:
        return self._numbers

    @property
    def blocks_everyone(self) -> bool:
        """True when the guard is on but nothing was allowlisted."""
        return self._enabled and not self._numbers

    def allows(self, phone: str) -> bool:
        if not self._enabled:
            return True
        candidate = normalize_phone(phone)
        if not candidate:
            return False
        if candidate in self._numbers:
            return True
        # Tolerate a missing or extra country code (a listed "9876543210"
        # should still match an inbound "919876543210" and vice versa).
        return any(
            allowed.endswith(candidate) or candidate.endswith(allowed)
            for allowed in self._numbers
            # Require a meaningful overlap so short numbers cannot match loosely.
            if min(len(allowed), len(candidate)) >= 10
        )

    def describe(self) -> str:
        if not self._enabled:
            return "disabled (every number can reach the bot)"
        if not self._numbers:
            return "enabled with an EMPTY list - the bot will reply to nobody"
        return f"enabled for {len(self._numbers)} number(s): {self.masked()}"

    def masked(self) -> str:
        return ", ".join(sorted(_mask(number) for number in self._numbers))

    def log_startup_banner(self) -> None:
        """Make the current posture impossible to miss in the logs."""
        if not self._enabled:
            logger.warning(
                "=" * 70
                + "\nWHATSAPP ALLOWLIST IS OFF - the bot will reply to ANY number "
                "that messages it.\nThis is correct only in production.\n"
                + "=" * 70
            )
        elif not self._numbers:
            logger.warning(
                "WhatsApp allowlist is ON but empty - every message will be "
                "ignored. Set WHATSAPP_ALLOWED_NUMBERS to your test number."
            )
        else:
            logger.info(
                "WhatsApp allowlist active - only %s can reach the bot",
                self.masked(),
            )


def _mask(phone: str) -> str:
    return f"{phone[:4]}***{phone[-3:]}" if len(phone) > 7 else "***"
