"""Callback-time parsing and business-hour validation."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.services.scheduling import CallbackTimeValidator, RejectionReason

IST = ZoneInfo("Asia/Kolkata")

#: Wednesday 5 August 2026, 12:00 - mid-week, mid-day, office open.
WEDNESDAY_NOON = datetime(2026, 8, 5, 12, 0, tzinfo=IST)


@pytest.fixture
def validator() -> CallbackTimeValidator:
    return CallbackTimeValidator(
        Settings(_env_file=None, business_timezone="Asia/Kolkata")
    )


@pytest.mark.parametrize(
    ("text", "expected_day", "expected_hour", "expected_minute"),
    [
        ("today 3pm", 5, 15, 0),
        ("today at 5:30 pm", 5, 17, 30),
        ("tomorrow 4pm", 6, 16, 0),
        ("tomorrow at 11:30am", 6, 11, 30),
        ("monday 11 am", 10, 11, 0),
        ("saturday 12:00", 8, 12, 0),
        ("08/08 3 pm", 8, 15, 0),
        ("10 aug 6pm", 10, 18, 0),
        ("aug 10 6pm", 10, 18, 0),
        # No meridiem: resolved towards business hours, so "4" means 4 PM.
        ("call me at 4", 5, 16, 0),
        ("today evening", 5, 17, 30),
        ("tomorrow morning", 6, 11, 30),
        ("tomorrow noon", 6, 12, 0),
    ],
)
def test_accepts_valid_times(
    validator: CallbackTimeValidator,
    text: str,
    expected_day: int,
    expected_hour: int,
    expected_minute: int,
) -> None:
    result = validator.parse(text, now=WEDNESDAY_NOON)

    assert result.ok, f"{text!r} was rejected: {result.reason}"
    assert result.slot is not None
    assert result.slot.at.day == expected_day
    assert result.slot.at.hour == expected_hour
    assert result.slot.at.minute == expected_minute


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("today 9am", RejectionReason.IN_PAST),
        ("tomorrow 9am", RejectionReason.OUTSIDE_HOURS),
        ("tomorrow 10pm", RejectionReason.OUTSIDE_HOURS),
        ("tomorrow 8:00", RejectionReason.OUTSIDE_HOURS),
        ("friday 2pm", RejectionReason.CLOSED_DAY),
        ("07/08 3pm", RejectionReason.CLOSED_DAY),  # 7 Aug 2026 is a Friday
        # Two days after Wednesday is Friday, so this is a closed day too.
        ("day after tomorrow 2pm", RejectionReason.CLOSED_DAY),
        ("tomorrow", RejectionReason.MISSING_TIME),
        ("monday", RejectionReason.MISSING_TIME),
        # No date and no time at all - nothing to work with.
        ("next week sometime", RejectionReason.NOT_UNDERSTOOD),
        ("purple elephant", RejectionReason.NOT_UNDERSTOOD),
        ("", RejectionReason.NOT_UNDERSTOOD),
    ],
)
def test_rejects_invalid_times(
    validator: CallbackTimeValidator, text: str, reason: RejectionReason
) -> None:
    result = validator.parse(text, now=WEDNESDAY_NOON)

    assert not result.ok
    assert result.reason is reason


def test_rejects_a_time_in_the_past(validator: CallbackTimeValidator) -> None:
    result = validator.parse("today 11:30 am", now=WEDNESDAY_NOON)

    assert not result.ok
    assert result.reason is RejectionReason.IN_PAST


def test_rejects_a_slot_too_close_to_now(validator: CallbackTimeValidator) -> None:
    """Default lead time is 30 minutes, so 12:10 is too soon at 12:00."""
    result = validator.parse("today 12:10 pm", now=WEDNESDAY_NOON)

    assert not result.ok
    assert result.reason is RejectionReason.TOO_SOON


def test_rejects_a_slot_beyond_the_horizon(validator: CallbackTimeValidator) -> None:
    result = validator.parse("10/12 3pm", now=WEDNESDAY_NOON)

    assert not result.ok
    assert result.reason is RejectionReason.TOO_FAR


def test_time_only_message_rolls_to_the_next_open_day(
    validator: CallbackTimeValidator,
) -> None:
    """11 AM has passed on Wednesday, and Friday is closed - so Saturday."""
    thursday_afternoon = datetime(2026, 8, 6, 15, 0, tzinfo=IST)

    result = validator.parse("11 am", now=thursday_afternoon)

    assert result.ok
    assert result.slot is not None
    assert result.slot.at.day == 8  # Saturday
    assert result.slot.at.weekday() == 5


def test_asap_returns_the_next_open_slot(validator: CallbackTimeValidator) -> None:
    result = validator.parse("asap", now=WEDNESDAY_NOON)

    assert result.ok
    assert result.slot is not None
    assert validator.is_within_business_hours(result.slot.at)
    assert result.slot.at >= WEDNESDAY_NOON


def test_asap_outside_hours_moves_to_the_next_open_day(
    validator: CallbackTimeValidator,
) -> None:
    thursday_night = datetime(2026, 8, 6, 23, 0, tzinfo=IST)

    result = validator.parse("anytime", now=thursday_night)

    assert result.ok
    assert result.slot is not None
    assert result.slot.at.weekday() != 4  # never a Friday
    assert validator.is_within_business_hours(result.slot.at)


def test_rejection_offers_valid_suggestions(validator: CallbackTimeValidator) -> None:
    result = validator.parse("friday 3pm", now=WEDNESDAY_NOON)

    assert not result.ok
    assert result.suggestions
    for suggestion in result.suggestions:
        assert validator.is_within_business_hours(suggestion.at)
        assert suggestion.at > WEDNESDAY_NOON


def test_business_hours_text_names_the_closed_day(
    validator: CallbackTimeValidator,
) -> None:
    text = validator.business_hours_text()

    assert "11 AM" in text
    assert "7 PM" in text
    assert "Friday" in text


def test_slot_display_is_human_readable(validator: CallbackTimeValidator) -> None:
    result = validator.parse("tomorrow 4pm", now=WEDNESDAY_NOON)

    assert result.slot is not None
    assert result.slot.display() == "Thursday, 6 August at 4 PM"


def test_closed_weekdays_are_configurable() -> None:
    """Sunday-closed instead of Friday-closed, via configuration only."""
    validator = CallbackTimeValidator(
        Settings(_env_file=None, business_closed_weekdays="sunday")
    )

    friday = validator.parse("friday 3pm", now=WEDNESDAY_NOON)
    sunday = validator.parse("sunday 3pm", now=WEDNESDAY_NOON)

    assert friday.ok
    assert not sunday.ok
    assert sunday.reason is RejectionReason.CLOSED_DAY
