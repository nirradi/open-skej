"""Tests for the hand-written canon.

Two things are being pinned here, and they fail for different reasons.

The **boundaries** are pinned because each one is a decision that could plausibly have gone the
other way: exactly-now, exactly-at-the-horizon, exactly-max-duration and exactly-at-closing all
pass, and the instant either side of each does not. A rule that is off by one at its bound is a rule
that refuses a booking a user can see is legal.

The **copy** is pinned because it is contract. The denial text crosses into the UI verbatim and the
end-to-end suite asserts the max-duration message as a full-string match, so a reworded sentence is
a broken build somewhere this package cannot see. That is why the expected strings are written out
in full below rather than built from the same helpers the rules use — deriving them would assert
only that the code agrees with itself.
"""

from datetime import datetime, time, timedelta, timezone

import pytest

from rules.canon import (
    DEFAULT_CANON,
    AvailabilityHoursRule,
    BookingHorizonRule,
    MaxDurationRule,
    NotInThePastRule,
    default_canon,
)
from rules.controller import evaluate_request
from rules.interfaces import (
    BookingRequest,
    CalendarContext,
    Context,
    RuleResult,
    UserContext,
    Weekday,
)

USER = "u1"
RESOURCE = "court-1"

#: Mid-morning, so a request built from it is comfortably inside default availability hours and a
#: denial can only have come from the rule under test.
NOW = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


def request(start_at: datetime, end_at: datetime) -> BookingRequest:
    return BookingRequest(user_id=USER, resource_id=RESOURCE, start_at=start_at, end_at=end_at)


def hours_from(moment: datetime, hours: float = 1) -> BookingRequest:
    return request(moment, moment + timedelta(hours=hours))


def context(now: datetime = NOW) -> Context:
    return Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=now),
    )


def at(hour: int, minute: int = 0, day: int = 20) -> datetime:
    """A UTC instant on the reference day. Availability hours are UTC hours."""
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


# --- NotInThePastRule ---------------------------------------------------------------


def test_a_booking_starting_exactly_now_is_allowed():
    """The bound is inclusive of the present instant."""
    assert NotInThePastRule().evaluate(hours_from(NOW), context()) == RuleResult.allow()


def test_a_booking_starting_a_minute_ago_is_denied():
    result = NotInThePastRule().evaluate(hours_from(NOW - timedelta(minutes=1)), context())
    assert not result.passed
    assert result.fail_reason == (
        "That time has already passed, so it can't be booked." " Please pick a time in the future."
    )


def test_a_booking_already_under_way_is_denied_on_its_start():
    """``end_at`` is never consulted: a booking in progress is still out of bounds."""
    started = NOW - timedelta(minutes=30)
    result = NotInThePastRule().evaluate(request(started, NOW + timedelta(hours=2)), context())
    assert not result.passed


# --- BookingHorizonRule -------------------------------------------------------------


def test_exactly_at_the_horizon_is_allowed():
    """The last bookable instant is the horizon itself, not the instant before it."""
    rule = BookingHorizonRule(days=60)
    assert rule.evaluate(hours_from(NOW + timedelta(days=60)), context()) == RuleResult.allow()


def test_a_second_past_the_horizon_is_denied():
    rule = BookingHorizonRule(days=60)
    result = rule.evaluate(hours_from(NOW + timedelta(days=60, seconds=1)), context())
    assert not result.passed
    assert result.fail_reason == (
        "Bookings can only be made up to 60 days ahead,"
        " and this one is further out than that."
        " Please pick an earlier date."
    )


def test_the_horizon_is_measured_from_start_at_only():
    """A booking that begins inside the horizon may run past it."""
    rule = BookingHorizonRule(days=60)
    start = NOW + timedelta(days=60) - timedelta(minutes=1)
    assert rule.evaluate(request(start, start + timedelta(hours=2)), context()).passed


def test_the_horizon_days_appear_in_the_copy():
    """The number is a constructor parameter, so the message must follow it."""
    result = BookingHorizonRule(days=7).evaluate(hours_from(NOW + timedelta(days=8)), context())
    assert "up to 7 days ahead" in (result.fail_reason or "")


def test_a_non_positive_horizon_is_rejected_at_construction():
    with pytest.raises(ValueError):
        BookingHorizonRule(days=0)


# --- MaxDurationRule ----------------------------------------------------------------


def test_exactly_max_duration_is_allowed():
    rule = MaxDurationRule(max_duration=timedelta(hours=2))
    assert rule.evaluate(hours_from(NOW, hours=2), context()) == RuleResult.allow()


def test_one_minute_over_max_duration_is_denied():
    rule = MaxDurationRule(max_duration=timedelta(hours=2))
    result = rule.evaluate(request(NOW, NOW + timedelta(hours=2, minutes=1)), context())
    assert not result.passed


def test_the_max_duration_denial_copy_is_exact():
    """Asserted verbatim: ``app/e2e/tests/03-sad-path.spec.ts`` does a full-string match on this.

    A substring assertion would pass against copy that had gained a prefix or lost its remedy
    sentence — the two ways this string breaks in practice.
    """
    rule = MaxDurationRule(max_duration=timedelta(hours=2))
    result = rule.evaluate(request(NOW, NOW + timedelta(hours=2, minutes=30)), context())
    assert result.fail_reason == (
        "Bookings can be at most 2 hours long, and this one is 2 hours and 30 minutes."
        " Please shorten it and try again."
    )


@pytest.mark.parametrize(
    "duration, rendered",
    [
        (timedelta(hours=1), "1 hour"),
        (timedelta(hours=2), "2 hours"),
        (timedelta(minutes=1), "1 minute"),
        (timedelta(minutes=45), "45 minutes"),
        (timedelta(hours=1, minutes=30), "1 hour and 30 minutes"),
        (timedelta(hours=3, minutes=1), "3 hours and 1 minute"),
    ],
)
def test_durations_are_rendered_the_way_a_person_says_them(duration, rendered):
    """Singular/plural and the " and " join are contract — the E2E suite mirrors this helper."""
    rule = MaxDurationRule(max_duration=duration)
    over = rule.evaluate(request(NOW, NOW + duration + timedelta(minutes=1)), context())
    assert f"at most {rendered} long" in (over.fail_reason or "")


def test_a_non_positive_max_duration_is_rejected_at_construction():
    with pytest.raises(ValueError):
        MaxDurationRule(max_duration=timedelta(0))


# --- AvailabilityHoursRule ----------------------------------------------------------


def hours_rule() -> AvailabilityHoursRule:
    return AvailabilityHoursRule(opens_at=time(6, 0), closes_at=time(23, 0))


def test_a_booking_starting_exactly_at_opening_is_allowed():
    assert hours_rule().evaluate(hours_from(at(6, 0)), context()).passed


def test_a_booking_starting_a_minute_before_opening_is_denied():
    result = hours_rule().evaluate(hours_from(at(5, 59)), context())
    assert not result.passed
    assert result.fail_reason == (
        "We open at 06:00, so this booking starts too early."
        " Please pick a time between 06:00 and 23:00."
    )


def test_a_booking_ending_exactly_at_closing_is_allowed():
    """The closing bound is inclusive."""
    assert hours_rule().evaluate(request(at(22, 0), at(23, 0)), context()).passed


def test_a_booking_ending_a_minute_after_closing_is_denied():
    result = hours_rule().evaluate(request(at(22, 0), at(23, 1)), context())
    assert not result.passed
    assert result.fail_reason == (
        "We close at 23:00, so this booking runs too late."
        " Please pick a time between 06:00 and 23:00."
    )


def test_a_booking_running_past_midnight_is_denied_not_wrapped():
    """Compared against a closing instant on ``start_at``'s date, not against a bare clock time.

    On clock times alone this booking ends at 00:30, which is "before" 23:00 by string of digits and
    would sail through as an early-morning slot on a day it never touches.
    """
    result = hours_rule().evaluate(request(at(23, 0, day=20), at(0, 30, day=21)), context())
    assert not result.passed
    assert "runs too late" in (result.fail_reason or "")


def test_availability_hours_are_utc_hours():
    """No local-timezone reading exists here: ``interfaces.py`` rejects a non-zero offset outright.

    07:00+02:00 is 05:00 UTC. The stub this rule was ported from judged wall-clock times as supplied
    and would have called this a 07:00 booking, safely inside a 06:00 opening; the engine sees the
    only instant there is and denies it.
    """
    local = timezone(timedelta(hours=2))
    with pytest.raises(ValueError):
        hours_from(datetime(2026, 7, 20, 7, 0, tzinfo=local))

    as_utc = datetime(2026, 7, 20, 7, 0, tzinfo=local).astimezone(timezone.utc)
    assert not hours_rule().evaluate(hours_from(as_utc), context()).passed


def test_opening_after_closing_is_rejected_at_construction():
    with pytest.raises(ValueError):
        AvailabilityHoursRule(opens_at=time(23, 0), closes_at=time(6, 0))


# --- The canon, in order ------------------------------------------------------------


def test_the_default_canon_is_the_four_rules_in_the_documented_order():
    assert [type(rule).__name__ for rule in DEFAULT_CANON] == [
        "NotInThePastRule",
        "BookingHorizonRule",
        "MaxDurationRule",
        "AvailabilityHoursRule",
    ]


def test_default_canon_builds_a_fresh_tuple_each_call():
    """Callers get their own instances, so per-Space configuration cannot alias a shared rule."""
    assert [type(r).__name__ for r in default_canon()] == [type(r).__name__ for r in DEFAULT_CANON]
    assert default_canon()[0] is not DEFAULT_CANON[0]


def test_a_date_denial_beats_a_duration_denial():
    """The remedy the message asks for must be the one that actually helps.

    An over-long booking two months past the horizon is refused for its date. Told to shorten it,
    the user would shorten it, resubmit, and be refused again.
    """
    far = NOW + timedelta(days=90)
    result = evaluate_request(hours_from(far, hours=3), context(), DEFAULT_CANON)
    assert "days ahead" in (result.fail_reason or "")


def test_a_past_denial_beats_a_duration_denial():
    yesterday = NOW - timedelta(days=1)
    result = evaluate_request(hours_from(yesterday, hours=3), context(), DEFAULT_CANON)
    assert "already passed" in (result.fail_reason or "")


def test_duration_is_reported_before_availability_hours():
    """An over-long booking that also runs past closing reports its length first."""
    result = evaluate_request(request(at(21, 30), at(23, 45)), context(), DEFAULT_CANON)
    assert "at most 2 hours" in (result.fail_reason or "")


def test_an_ordinary_booking_passes_the_whole_canon():
    result = evaluate_request(hours_from(NOW + timedelta(days=1)), context(), DEFAULT_CANON)
    assert result == RuleResult.allow()
