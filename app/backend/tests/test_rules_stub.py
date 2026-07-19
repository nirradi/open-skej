"""Tests for the stub rule engine, focused on the boundary directions."""

from datetime import datetime, timedelta, timezone

import pytest

from app.rules_stub import (
    AVAILABILITY_CLOSE,
    AVAILABILITY_OPEN,
    BOOKING_HORIZON_DAYS,
    MAX_BOOKING_DURATION,
    BookingRequest,
    Context,
    RuleResult,
)
from app.rules_stub import evaluate as _evaluate

DAY = datetime(2026, 7, 20, tzinfo=timezone.utc)

# A clock pinned a day before DAY, so every fixed date in this module sits inside
# the booking horizon. Without this the date rules would judge these cases
# against the wall clock and the whole file would start failing on 2026-07-21.
NOW = DAY - timedelta(days=1)
FIXED_CLOCK = Context(now=NOW)


def evaluate(booking: BookingRequest, context: Context | None = None) -> RuleResult:
    """``rules_stub.evaluate`` with the clock pinned, unless a Context is given.

    Cases that are *about* the clock pass their own Context and are untouched by
    the substitution.
    """
    return _evaluate(booking, context if context is not None else FIXED_CLOCK)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


def request(start: datetime, end: datetime) -> BookingRequest:
    return BookingRequest(start_at=start, end_at=end)


def test_booking_inside_hours_and_under_max_duration_is_allowed():
    result = evaluate(request(at(10), at(11)))

    assert result.allowed
    assert result.message


def test_booking_longer_than_max_duration_is_denied():
    result = evaluate(request(at(10), at(13)))

    assert not result.allowed
    assert "2 hours" in result.message


def test_booking_of_exactly_max_duration_is_allowed():
    """The duration limit is inclusive: exactly 2 hours is fine, 2h01 is not."""
    start = at(10)

    assert evaluate(request(start, start + MAX_BOOKING_DURATION)).allowed
    assert not evaluate(request(start, start + MAX_BOOKING_DURATION + timedelta(minutes=1))).allowed


def test_booking_starting_before_opening_is_denied():
    result = evaluate(request(at(5), at(6, 30)))

    assert not result.allowed
    assert "06:00" in result.message


def test_booking_starting_exactly_at_opening_is_allowed():
    """The opening bound is inclusive: 06:00 is open, 05:59 is not."""
    start = datetime.combine(DAY.date(), AVAILABILITY_OPEN, timezone.utc)

    assert evaluate(request(start, start + timedelta(hours=1))).allowed
    assert not evaluate(
        request(start - timedelta(minutes=1), start + timedelta(minutes=30))
    ).allowed


def test_booking_ending_after_closing_is_denied():
    result = evaluate(request(at(22), at(23, 30)))

    assert not result.allowed
    assert "23:00" in result.message


def test_booking_ending_exactly_at_closing_is_allowed():
    """The closing bound is inclusive: ending at 23:00 is fine, 23:01 is not."""
    closing = datetime.combine(DAY.date(), AVAILABILITY_CLOSE, timezone.utc)

    assert evaluate(request(closing - timedelta(hours=1), closing)).allowed
    assert not evaluate(
        request(closing - timedelta(hours=1), closing + timedelta(minutes=1))
    ).allowed


def test_booking_running_past_midnight_is_denied():
    """A wrap-around must not look like an early-morning booking inside hours."""
    result = evaluate(request(at(22, 30), at(24, 30)))

    assert not result.allowed
    assert "23:00" in result.message


def test_duration_is_checked_before_availability_hours():
    """An over-long booking that is also out of hours reports the length first."""
    result = evaluate(request(at(22), at(25)))

    assert not result.allowed
    assert "2 hours" in result.message


def test_denial_messages_are_human_readable():
    for booking in (request(at(10), at(13)), request(at(5), at(5, 30))):
        message = evaluate(booking).message

        assert message.endswith(".")
        assert message[0].isupper()
        assert "Error" not in message
        assert "Traceback" not in message


def test_non_utc_offsets_are_judged_in_their_own_timezone():
    """09:00 local is inside hours even though it is 02:00 UTC."""
    local = timezone(timedelta(hours=7))
    start = datetime(2026, 7, 20, 9, 0, tzinfo=local)

    assert evaluate(request(start, start + timedelta(hours=1))).allowed


def test_history_is_accepted_and_ignored_by_the_stub():
    """The Context parameter exists for Stream 3; the stub's rules are local."""
    booking = request(at(10), at(11))
    history = Context(now=NOW, history=(request(at(8), at(9)), request(at(12), at(13))))

    assert evaluate(booking, history).allowed
    assert evaluate(booking, history) == evaluate(booking)


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        BookingRequest(start_at=datetime(2026, 7, 20, 10), end_at=at(11))


def test_non_positive_interval_is_rejected():
    with pytest.raises(ValueError):
        BookingRequest(start_at=at(11), end_at=at(11))


# --- Booking horizon (task 1.4b) -------------------------------------------
#
# Every case below injects an explicit clock. None of them may consult the wall
# clock, or they would pass or fail depending on the day the suite is run.

CLOCK = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
HORIZON = Context(now=CLOCK)


def hours_from(moment: datetime, hours: int = 1) -> BookingRequest:
    """A one-hour booking starting at ``moment``, inside availability hours.

    Anchoring on 10:00 keeps every horizon case clear of the duration and
    opening-hours rules, so a denial here can only have come from a date rule.
    """
    return request(moment, moment + timedelta(hours=hours))


def test_booking_starting_now_is_allowed():
    """The present instant is bookable — the past bound excludes only what's gone."""
    result = evaluate(hours_from(CLOCK), HORIZON)

    assert result.allowed


def test_booking_starting_one_minute_in_the_past_is_denied():
    result = evaluate(hours_from(CLOCK - timedelta(minutes=1)), HORIZON)

    assert not result.allowed
    assert "already passed" in result.message


def test_booking_exactly_at_the_horizon_is_allowed():
    """Exactly BOOKING_HORIZON_DAYS ahead is the last bookable instant."""
    result = evaluate(hours_from(CLOCK + timedelta(days=BOOKING_HORIZON_DAYS)), HORIZON)

    assert result.allowed


def test_booking_one_minute_past_the_horizon_is_denied():
    start = CLOCK + timedelta(days=BOOKING_HORIZON_DAYS, minutes=1)

    result = evaluate(hours_from(start), HORIZON)

    assert not result.allowed
    assert str(BOOKING_HORIZON_DAYS) in result.message


def test_horizon_message_names_the_limit_from_the_constant():
    """The copy must track BOOKING_HORIZON_DAYS, not restate it as a literal."""
    far = hours_from(CLOCK + timedelta(days=BOOKING_HORIZON_DAYS * 2))

    message = evaluate(far, HORIZON).message

    assert f"{BOOKING_HORIZON_DAYS} days" in message


def test_date_rules_are_checked_before_duration_and_hours():
    """A booking that is out of range *and* over-long reports the range first.

    Ordering matters here: "shorten it" is unactionable advice for a booking
    whose real problem is the date, and the engine only ever returns one message.
    """
    over_long_and_too_far = request(
        CLOCK + timedelta(days=BOOKING_HORIZON_DAYS + 1),
        CLOCK + timedelta(days=BOOKING_HORIZON_DAYS + 1, hours=3),
    )
    yesterday = CLOCK - timedelta(days=1)
    over_long_and_past = request(yesterday, yesterday + timedelta(hours=3))

    assert f"{BOOKING_HORIZON_DAYS} days" in evaluate(over_long_and_too_far, HORIZON).message
    assert "already passed" in evaluate(over_long_and_past, HORIZON).message


def test_date_rule_messages_are_human_readable():
    for booking in (
        hours_from(CLOCK - timedelta(days=1)),
        hours_from(CLOCK + timedelta(days=BOOKING_HORIZON_DAYS + 1)),
    ):
        message = evaluate(booking, HORIZON).message

        assert message.endswith(".")
        assert message[0].isupper()
        assert "Error" not in message
        assert "Traceback" not in message


def test_clock_defaults_to_the_current_time():
    """The default must be live, or production would judge against a frozen clock."""
    # Pinned to 10:00 so the availability-hours rule can't decide the outcome
    # when the suite happens to run late at night.
    midmorning = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)

    assert not evaluate(hours_from(midmorning - timedelta(days=2)), Context()).allowed
    assert evaluate(hours_from(midmorning + timedelta(days=1)), Context()).allowed


def test_naive_clock_is_rejected():
    with pytest.raises(ValueError):
        Context(now=datetime(2026, 7, 19, 10, 0))
