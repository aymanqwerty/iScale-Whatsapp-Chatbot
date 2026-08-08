"""Cross-checks between the knowledge files and the runtime configuration.

Business hours are currently stated in four places: `BUSINESS_OPEN_TIME` /
`BUSINESS_CLOSE_TIME` in the environment, and again in `callback_rules.json`,
`chatbot_rules.json` and `policies.json`.

Only the environment drives behaviour - `CallbackTimeValidator` accepts or
rejects a requested slot from `Settings` alone. The JSON copies are what the
model reads out to the user. So when they drift, nothing breaks loudly: the bot
simply tells people one thing and enforces another, which surfaces as a customer
being told "any time before 7 PM" and then having 6:45 PM rejected.

These checks do not resolve the disagreement - they report it at startup so a
human can. Making the JSON authoritative instead would mean moving validation
off typed, validated settings onto free-text files, which is a worse trade.
"""

from __future__ import annotations

from datetime import time
from typing import Any

from app.core.config import Settings
from app.services.knowledge.loader import KnowledgeBase

_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _parse_time(value: Any) -> time | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        hour, _, minute = text.partition(":")
        return time(int(hour), int(minute or 0))
    except ValueError:
        return None


def _weekday_numbers(values: Any) -> frozenset[int] | None:
    if not isinstance(values, list):
        return None
    days: set[int] = set()
    for value in values:
        key = str(value).strip().lower()
        if key not in _WEEKDAYS:
            return None
        days.add(_WEEKDAYS[key])
    return frozenset(days)


def check_business_hours(
    knowledge_base: KnowledgeBase, settings: Settings
) -> list[str]:
    """Return a warning per knowledge file that disagrees with `Settings`."""
    warnings: list[str] = []

    for name, hours, weekly_off in _declared_hours(knowledge_base):
        opens = _parse_time(hours.get("start") or hours.get("start_time"))
        closes = _parse_time(hours.get("end") or hours.get("end_time"))

        if opens is not None and opens != settings.business_open_time:
            warnings.append(
                f"{name} says the day starts at {opens:%H:%M}, but "
                f"BUSINESS_OPEN_TIME is {settings.business_open_time:%H:%M}"
            )
        if closes is not None and closes != settings.business_close_time:
            warnings.append(
                f"{name} says the day ends at {closes:%H:%M}, but "
                f"BUSINESS_CLOSE_TIME is {settings.business_close_time:%H:%M}"
            )

        declared_off = _weekday_numbers(weekly_off)
        if declared_off is not None and declared_off != settings.closed_weekdays:
            warnings.append(
                f"{name} closes on {sorted(declared_off)}, but "
                f"BUSINESS_CLOSED_WEEKDAYS resolves to {sorted(settings.closed_weekdays)}"
            )

    return warnings


def _declared_hours(
    knowledge_base: KnowledgeBase,
) -> list[tuple[str, dict[str, Any], Any]]:
    """Every (source name, hours block, weekly-off list) worth checking."""
    found: list[tuple[str, dict[str, Any], Any]] = []

    callback = knowledge_base.rules.get("callback")
    if isinstance(callback, dict) and isinstance(callback.get("business_hours"), dict):
        found.append(
            ("chatbot_rules.json", callback["business_hours"], callback.get("weekly_off"))
        )

    for filename, document in knowledge_base.documents.items():
        if not isinstance(document, dict):
            continue
        hours = document.get("working_hours")
        weekly_off = document.get("weekly_off")
        # Either half is worth checking on its own: a file may state only the
        # closed days, and skipping it would silently drop that comparison.
        if isinstance(hours, dict) or weekly_off is not None:
            found.append((filename, hours if isinstance(hours, dict) else {}, weekly_off))

    return found
