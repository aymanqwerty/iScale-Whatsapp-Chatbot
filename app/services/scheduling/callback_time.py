"""Parse and validate a requested callback time.

Deliberately *not* an LLM job. Scheduling is a rules problem with a right
answer - business hours, closed days, no callbacks in the past - and a
deterministic parser is testable, instant, free, and cannot hallucinate a slot
on a day the office is shut.

The parser understands the shapes people actually type on WhatsApp:

    "tomorrow 4pm"      "today evening"     "monday at 11:30 am"
    "12/08 3 pm"        "after 5"           "asap"
    "day after tomorrow morning"            "15 aug 2:30pm"

Anything it cannot read confidently is rejected rather than guessed at, and the
bot asks again with examples.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class RejectionReason(StrEnum):
    """Why a requested time was not accepted."""

    NOT_UNDERSTOOD = "NOT_UNDERSTOOD"
    MISSING_TIME = "MISSING_TIME"
    IN_PAST = "IN_PAST"
    TOO_SOON = "TOO_SOON"
    TOO_FAR = "TOO_FAR"
    CLOSED_DAY = "CLOSED_DAY"
    OUTSIDE_HOURS = "OUTSIDE_HOURS"


@dataclass(frozen=True, slots=True)
class CallbackSlot:
    """An accepted callback moment, timezone-aware in the business timezone."""

    at: datetime
    raw: str

    def to_utc(self) -> datetime:
        return self.at.astimezone(UTC)

    def display(self) -> str:
        """Human-friendly rendering for the confirmation message."""
        # %-d / %-I are not portable to Windows, so format the numbers by hand.
        day = self.at.day
        hour12 = self.at.hour % 12 or 12
        minute = f":{self.at.minute:02d}" if self.at.minute else ""
        meridiem = "AM" if self.at.hour < 12 else "PM"
        return (
            f"{self.at.strftime('%A')}, {day} {self.at.strftime('%B')} "
            f"at {hour12}{minute} {meridiem}"
        )


@dataclass(frozen=True, slots=True)
class CallbackParseResult:
    """Outcome of parsing one user message as a callback time."""

    slot: CallbackSlot | None = None
    reason: RejectionReason | None = None
    #: The date the user named, kept even when the slot was rejected.
    #: "10 August at 4:30 am" is refused for being before opening time, but the
    #: user did say the 10th - and a follow-up "4:30 pm then" must land on that
    #: day, not today. Without this the booking silently moved to the wrong date.
    parsed_date: date | None = None
    #: Populated on rejection - the next few open slots, to offer as examples.
    suggestions: tuple[CallbackSlot, ...] = ()

    @property
    def ok(self) -> bool:
        return self.slot is not None


# --------------------------------------------------------------------------- #
# Lexicon
# --------------------------------------------------------------------------- #

_WEEKDAYS: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

#: Vague times of day, mapped to a concrete slot inside working hours.
_TIME_WORDS: dict[str, time] = {
    "morning": time(11, 30),
    "noon": time(12, 0),
    "midday": time(12, 0),
    "lunch": time(13, 0),
    "afternoon": time(14, 0),
    "evening": time(17, 30),
    "night": time(18, 30),
}

_ASAP_WORDS = ("asap", "anytime", "any time", "as soon as possible", "right now", "immediately")

_TIME_HH_MM = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm|a\.m\.|p\.m\.)?\b")
_TIME_H_MERIDIEM = re.compile(r"\b(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)\b")
_TIME_OCLOCK = re.compile(r"\b(\d{1,2})\s*o'?\s*clock\b")
_TIME_BARE = re.compile(r"\b(?:at|around|by|after|before)\s+(\d{1,2})\b")

_DATE_NUMERIC = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b")
_DATE_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")\b"
)
_DATE_MONTH_DAY = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b"
)


class CallbackTimeValidator:
    """Turns free text into a validated callback slot."""

    def __init__(self, settings: Settings) -> None:
        self._tz: ZoneInfo = settings.tz
        self._open: time = settings.business_open_time
        self._close: time = settings.business_close_time
        self._closed_weekdays: frozenset[int] = settings.closed_weekdays
        self._max_days_ahead: int = settings.callback_max_days_ahead
        self._min_lead = timedelta(minutes=settings.callback_min_lead_minutes)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def now(self) -> datetime:
        return datetime.now(self._tz)

    def parse(
        self,
        text: str,
        *,
        now: datetime | None = None,
        assume_date: date | None = None,
    ) -> CallbackParseResult:
        """Parse `text` into a validated slot, or explain why it was rejected.

        `assume_date` is the date the user gave on a previous, rejected attempt.
        It is used only when this message carries a time but no date, so
        "10 August at 4:30 am" followed by "4:30 pm then" books the 10th rather
        than silently jumping to today.
        """
        reference = (now or self.now()).astimezone(self._tz)
        cleaned = text.strip().lower()

        if not cleaned:
            return self._reject(RejectionReason.NOT_UNDERSTOOD, reference)

        if any(word in cleaned for word in _ASAP_WORDS):
            earliest = self._next_open_slot(reference)
            return CallbackParseResult(slot=CallbackSlot(at=earliest, raw=text.strip()))

        target_date, date_found = self._parse_date(cleaned, reference)
        target_time = self._parse_time(cleaned)

        if target_time is None:
            # A bare date ("tomorrow") is understood but unusable - we need an hour.
            reason = (
                RejectionReason.MISSING_TIME if date_found else RejectionReason.NOT_UNDERSTOOD
            )
            rejected = self._reject(reason, reference)
            return replace(rejected, parsed_date=target_date if date_found else None)

        if not date_found:
            if assume_date is not None and assume_date >= reference.date():
                target_date = assume_date
                date_found = True
            else:
                # Time only ("4 pm"): today if that is still reachable, else the
                # next working day at the same time.
                target_date = self._resolve_implicit_date(target_time, reference)

        candidate = datetime.combine(target_date, target_time, tzinfo=self._tz)
        result = self._validate(candidate, text.strip(), reference)
        return replace(result, parsed_date=target_date if date_found else None)

    def is_within_business_hours(self, moment: datetime) -> bool:
        local = moment.astimezone(self._tz)
        if local.weekday() in self._closed_weekdays:
            return False
        return self._open <= local.time() <= self._close

    def business_hours_text(self) -> str:
        closed = self._closed_day_names()
        base = f"{_fmt_time(self._open)} to {_fmt_time(self._close)}"
        if closed:
            return f"{base} ({', '.join(closed)} closed)"
        return base

    def suggest_slots(self, *, now: datetime | None = None, count: int = 3) -> list[CallbackSlot]:
        """A few concrete, valid slots to show the user after a rejection."""
        reference = (now or self.now()).astimezone(self._tz)
        suggestions: list[CallbackSlot] = []
        cursor = self._next_open_slot(reference)
        preferred = (time(11, 30), time(14, 0), time(17, 0))

        day = cursor.date()
        guard = 0
        while len(suggestions) < count and guard < 30:
            guard += 1
            if day.weekday() not in self._closed_weekdays:
                for slot_time in preferred:
                    moment = datetime.combine(day, slot_time, tzinfo=self._tz)
                    if moment >= reference + self._min_lead:
                        suggestions.append(CallbackSlot(at=moment, raw=""))
                        break
            day += timedelta(days=1)
        return suggestions[:count]

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def _validate(
        self, candidate: datetime, raw: str, reference: datetime
    ) -> CallbackParseResult:
        if candidate < reference:
            return self._reject(RejectionReason.IN_PAST, reference)
        if candidate < reference + self._min_lead:
            return self._reject(RejectionReason.TOO_SOON, reference)
        if candidate > reference + timedelta(days=self._max_days_ahead):
            return self._reject(RejectionReason.TOO_FAR, reference)
        if candidate.weekday() in self._closed_weekdays:
            return self._reject(RejectionReason.CLOSED_DAY, reference)
        if not (self._open <= candidate.time() <= self._close):
            return self._reject(RejectionReason.OUTSIDE_HOURS, reference)
        return CallbackParseResult(slot=CallbackSlot(at=candidate, raw=raw))

    def _reject(self, reason: RejectionReason, reference: datetime) -> CallbackParseResult:
        return CallbackParseResult(
            reason=reason,
            suggestions=tuple(self.suggest_slots(now=reference)),
        )

    # ------------------------------------------------------------------ #
    # Date parsing
    # ------------------------------------------------------------------ #
    def _parse_date(self, text: str, reference: datetime) -> tuple[date, bool]:
        """Return (date, was_explicit). Falls back to today when not stated."""
        today = reference.date()

        # Order matters: "day after tomorrow" must be tested before "tomorrow".
        if "day after tomorrow" in text or "day after tmrw" in text:
            return today + timedelta(days=2), True
        if re.search(r"\b(tomorrow|tomorow|tommorow|tmrw|tmr|kal)\b", text):
            return today + timedelta(days=1), True
        if re.search(r"\btoday\b|\baaj\b|\btonight\b", text):
            return today, True

        match = _DATE_NUMERIC.search(text)
        if match:
            resolved = self._from_numeric_date(match, today)
            if resolved is not None:
                return resolved, True

        match = _DATE_DAY_MONTH.search(text)
        if match:
            resolved = self._from_day_month(int(match.group(1)), _MONTHS[match.group(2)], today)
            if resolved is not None:
                return resolved, True

        match = _DATE_MONTH_DAY.search(text)
        if match:
            resolved = self._from_day_month(int(match.group(2)), _MONTHS[match.group(1)], today)
            if resolved is not None:
                return resolved, True

        for name, weekday in _WEEKDAYS.items():
            if re.search(rf"\b{name}\b", text):
                return self._next_weekday(today, weekday, force_next="next" in text), True

        return today, False

    def _from_numeric_date(self, match: re.Match[str], today: date) -> date | None:
        """Interpret d/m/[y] - day-first, as used in India."""
        day, month = int(match.group(1)), int(match.group(2))
        year_part = match.group(3)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        if year_part:
            year = int(year_part)
            if year < 100:
                year += 2000
        else:
            year = today.year
        try:
            resolved = date(year, month, day)
        except ValueError:
            return None
        # A bare "03/01" late in the year almost certainly means next year.
        if not year_part and resolved < today:
            try:
                resolved = date(year + 1, month, day)
            except ValueError:
                return None
        return resolved

    def _from_day_month(self, day: int, month: int, today: date) -> date | None:
        try:
            resolved = date(today.year, month, day)
        except ValueError:
            return None
        if resolved < today:
            try:
                resolved = date(today.year + 1, month, day)
            except ValueError:
                return None
        return resolved

    @staticmethod
    def _next_weekday(today: date, weekday: int, *, force_next: bool) -> date:
        delta = (weekday - today.weekday()) % 7
        if delta == 0 or force_next:
            # "monday" said on a Monday means the coming Monday, not today.
            delta = delta or 7
            if force_next and delta < 7 and (weekday - today.weekday()) % 7 == 0:
                delta = 7
        return today + timedelta(days=delta)

    def _resolve_implicit_date(self, target_time: time, reference: datetime) -> date:
        """Pick the day for a time-only message such as "4 pm"."""
        today = reference.date()
        same_day = datetime.combine(today, target_time, tzinfo=self._tz)
        if (
            same_day >= reference + self._min_lead
            and today.weekday() not in self._closed_weekdays
            and self._open <= target_time <= self._close
        ):
            return today
        cursor = today + timedelta(days=1)
        for _ in range(14):
            if cursor.weekday() not in self._closed_weekdays:
                return cursor
            cursor += timedelta(days=1)
        return cursor

    # ------------------------------------------------------------------ #
    # Time parsing
    # ------------------------------------------------------------------ #
    def _parse_time(self, text: str) -> time | None:
        match = _TIME_HH_MM.search(text)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if minute > 59:
                return None
            hour = self._apply_meridiem(hour, match.group(3))
            return time(hour, minute) if 0 <= hour <= 23 else None

        match = _TIME_H_MERIDIEM.search(text)
        if match:
            hour = self._apply_meridiem(int(match.group(1)), match.group(2))
            return time(hour, 0) if 0 <= hour <= 23 else None

        match = _TIME_OCLOCK.search(text)
        if match:
            resolved_hour = self._disambiguate_hour(int(match.group(1)))
            return time(resolved_hour, 0) if resolved_hour is not None else None

        for word, resolved in _TIME_WORDS.items():
            if word in text:
                return resolved

        match = _TIME_BARE.search(text)
        if match:
            resolved_hour = self._disambiguate_hour(int(match.group(1)))
            return time(resolved_hour, 0) if resolved_hour is not None else None

        # A lone number is only a time if it is the whole message ("4").
        stripped = text.strip()
        if stripped.isdigit() and len(stripped) <= 2:
            resolved_hour = self._disambiguate_hour(int(stripped))
            return time(resolved_hour, 0) if resolved_hour is not None else None

        return None

    @staticmethod
    def _apply_meridiem(hour: int, meridiem: str | None) -> int:
        if not meridiem:
            return hour
        is_pm = meridiem.replace(".", "").startswith("p")
        if is_pm and hour < 12:
            return hour + 12
        if not is_pm and hour == 12:
            return 0
        return hour

    def _disambiguate_hour(self, hour: int) -> int | None:
        """Resolve "at 4" with no am/pm, preferring a time the office is open.

        Users say "call me at 4" meaning 4 PM. Rather than hard-coding that, try
        both readings and keep whichever lands inside business hours.
        """
        if not 0 <= hour <= 23:
            return None
        candidates = [hour] if hour > 12 else [hour, hour + 12]
        for candidate in candidates:
            if candidate <= 23 and self._open <= time(candidate, 0) <= self._close:
                return candidate
        return hour if hour <= 23 else None

    # ------------------------------------------------------------------ #
    def _next_open_slot(self, reference: datetime) -> datetime:
        """Earliest bookable moment from `reference`, respecting lead time."""
        cursor = _round_up_half_hour(reference + self._min_lead)
        for _ in range(60):
            if cursor.weekday() in self._closed_weekdays or cursor.time() > self._close:
                cursor = datetime.combine(
                    cursor.date() + timedelta(days=1), self._open, tzinfo=self._tz
                )
                continue
            if cursor.time() < self._open:
                cursor = datetime.combine(cursor.date(), self._open, tzinfo=self._tz)
                continue
            return cursor
        return cursor  # pragma: no cover - only if every day is a closed day

    def _closed_day_names(self) -> list[str]:
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        return [names[d] for d in sorted(self._closed_weekdays)]


def _round_up_half_hour(moment: datetime) -> datetime:
    """Snap forward to the next :00 or :30 so suggested slots look deliberate."""
    moment = moment.replace(second=0, microsecond=0)
    if moment.minute in (0, 30):
        return moment
    if moment.minute < 30:
        return moment.replace(minute=30)
    return (moment + timedelta(hours=1)).replace(minute=0)


def _fmt_time(value: time) -> str:
    hour12 = value.hour % 12 or 12
    minute = f":{value.minute:02d}" if value.minute else ""
    meridiem = "AM" if value.hour < 12 else "PM"
    return f"{hour12}{minute} {meridiem}"
