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
    SlotAlignmentRule,
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
from tests.frames import utc_frame

USER = "u1"
RESOURCE = "court-1"

#: Mid-morning, so a request built from it is comfortably inside default availability hours and a
#: denial can only have come from the rule under test.
NOW = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


def request(start_at: datetime, end_at: datetime) -> BookingRequest:
    return BookingRequest(user_id=USER, resource_id=RESOURCE, start_at=start_at, end_at=end_at)


def hours_from(moment: datetime, hours: float = 1) -> BookingRequest:
    return request(moment, moment + timedelta(hours=hours))


def context(now: datetime = NOW, *, frame_for: datetime | None = None) -> Context:
    """A context for a UTC venue, its local frame resolved for ``frame_for``'s own day.

    Every rule here reads only the request and its own parameters, so the frame is inert for them
    — but ``evaluate_request`` cross-checks it against the request's start, so a test running the
    whole canon against a booking on another day says which day that is.
    """
    return Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=now),
        local=utc_frame(frame_for if frame_for is not None else now),
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


# --- SlotAlignmentRule ----------------------------------------------------------------

#: Midnight on the reference day, in UTC — the shape the adapter always hands this rule: the
#: Space's own local midnight for the booking's date, already converted to UTC.
MIDNIGHT = datetime(2026, 7, 20, tzinfo=timezone.utc)


def slot_rule(slot_minutes: int = 30, anchor: datetime = MIDNIGHT) -> SlotAlignmentRule:
    return SlotAlignmentRule(slot_minutes=slot_minutes, anchor=anchor)


def test_a_start_and_end_both_on_the_grid_is_allowed():
    assert slot_rule().evaluate(request(at(10, 0), at(10, 30)), context()).passed


def test_an_off_grid_start_is_denied():
    result = slot_rule().evaluate(request(at(10, 7), at(10, 30)), context())
    assert not result.passed
    assert "30-minute grid" in (result.fail_reason or "")


def test_an_off_grid_end_with_an_otherwise_aligned_start_is_denied():
    """Both bounds are checked: an aligned start does not excuse an off-grid end."""
    result = slot_rule().evaluate(request(at(10, 0), at(10, 22)), context())
    assert not result.passed


def test_a_booking_starting_and_ending_several_slots_later_is_allowed():
    """The exact boundary case: both bounds fall on slot lines several slots apart, not just one."""
    assert slot_rule().evaluate(request(at(10, 0), at(11, 30)), context()).passed


def test_the_denial_copy_names_the_grid_size():
    result = slot_rule(slot_minutes=45).evaluate(request(at(10, 0), at(10, 20)), context())
    assert not result.passed
    assert "45-minute grid" in (result.fail_reason or "")


def test_a_slot_minutes_that_does_not_divide_a_day_is_rejected_at_construction():
    """1440 must be a whole number of slots; 7 does not divide it."""
    with pytest.raises(ValueError):
        SlotAlignmentRule(slot_minutes=7, anchor=MIDNIGHT)


def test_a_non_positive_slot_minutes_is_rejected_at_construction():
    with pytest.raises(ValueError):
        SlotAlignmentRule(slot_minutes=0, anchor=MIDNIGHT)


def test_a_naive_anchor_is_rejected_at_construction():
    with pytest.raises(ValueError):
        SlotAlignmentRule(slot_minutes=30, anchor=datetime(2026, 7, 20))


def test_a_non_utc_anchor_offset_is_rejected_at_construction():
    with pytest.raises(ValueError):
        SlotAlignmentRule(
            slot_minutes=30, anchor=datetime(2026, 7, 20, tzinfo=timezone(timedelta(hours=2)))
        )


def test_the_grid_is_anchored_on_the_supplied_instant_not_on_utc_midnight():
    """A non-midnight anchor still defines a valid grid — the rule never assumes its anchor is
    00:00."""
    offset_anchor = MIDNIGHT + timedelta(minutes=15)
    rule = slot_rule(slot_minutes=30, anchor=offset_anchor)

    assert rule.evaluate(request(at(10, 15), at(10, 45)), context()).passed
    assert not rule.evaluate(request(at(10, 0), at(10, 30)), context()).passed


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


def test_equal_opening_and_closing_is_rejected_at_construction():
    with pytest.raises(ValueError):
        AvailabilityHoursRule(opens_at=time(6, 0), closes_at=time(6, 0))


# --- AvailabilityHoursRule, crossing a UTC calendar day ------------------------------
#
# `opens_at > closes_at` is not a construction error: it is the shape a Space's local
# operating hours resolve to whenever the zone's offset is large enough to push local
# morning back across UTC midnight (an entirely ordinary Sydney or Honolulu schedule,
# not an exotic one — see `app.operating_hours` and `DEFERRED.md` items 16/17). This
# rule has to read that inversion correctly rather than reject it.


def wrapping_hours_rule() -> AvailabilityHoursRule:
    """Opens at 23:00 UTC, closes at 11:00 UTC the following date — a crossing window."""
    return AvailabilityHoursRule(opens_at=time(23, 0), closes_at=time(11, 0))


def test_a_crossing_window_constructs_without_error():
    rule = wrapping_hours_rule()
    assert rule.opens_at == time(23, 0)
    assert rule.closes_at == time(11, 0)


def test_a_crossing_window_allows_a_booking_shortly_after_opening():
    """23:30 on day 20 is the "opens today, closes tomorrow" half of one occurrence."""
    result = wrapping_hours_rule().evaluate(hours_from(at(23, 30, day=20)), context())
    assert result.passed


def test_a_crossing_window_allows_a_booking_shortly_before_closing():
    """10:00-11:00 on day 21 is the "opened yesterday, closes today" half of the same occurrence."""
    result = wrapping_hours_rule().evaluate(
        request(at(10, 0, day=21), at(11, 0, day=21)), context()
    )
    assert result.passed


def test_a_crossing_window_denies_a_booking_in_the_daily_gap():
    """15:00 sits strictly between closing (11:00) and the next opening (23:00) — never in hours.

    This is the case that used to raise `MidnightWrapError` and deny every booking on the Space
    with the engine's generic copy, regardless of when it actually fell (`DEFERRED.md` item 16's
    mislabelling, and item 17's total failure). Denied here for the specific, actionable reason.
    """
    result = wrapping_hours_rule().evaluate(hours_from(at(15, 0, day=20)), context())
    assert not result.passed
    assert "starts too early" in (result.fail_reason or "")


def test_a_crossing_window_denies_a_booking_that_runs_past_the_wrapped_close():
    result = wrapping_hours_rule().evaluate(
        request(at(10, 30, day=21), at(11, 30, day=21)), context()
    )
    assert not result.passed
    assert "runs too late" in (result.fail_reason or "")


def test_a_crossing_window_closing_bound_is_inclusive():
    """Ending exactly at the wrapped closing instant is fine, one minute later is not."""
    assert (
        wrapping_hours_rule()
        .evaluate(request(at(10, 30, day=21), at(11, 0, day=21)), context())
        .passed
    )
    assert (
        not wrapping_hours_rule()
        .evaluate(request(at(10, 30, day=21), at(11, 1, day=21)), context())
        .passed
    )


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
    result = evaluate_request(hours_from(far, hours=3), context(frame_for=far), DEFAULT_CANON)
    assert "days ahead" in (result.fail_reason or "")


def test_a_past_denial_beats_a_duration_denial():
    yesterday = NOW - timedelta(days=1)
    result = evaluate_request(
        hours_from(yesterday, hours=3), context(frame_for=yesterday), DEFAULT_CANON
    )
    assert "already passed" in (result.fail_reason or "")


def test_duration_is_reported_before_availability_hours():
    """An over-long booking that also runs past closing reports its length first."""
    result = evaluate_request(
        request(at(21, 30), at(23, 45)), context(frame_for=at(21, 30)), DEFAULT_CANON
    )
    assert "at most 2 hours" in (result.fail_reason or "")


def test_an_ordinary_booking_passes_the_whole_canon():
    tomorrow = NOW + timedelta(days=1)
    result = evaluate_request(hours_from(tomorrow), context(frame_for=tomorrow), DEFAULT_CANON)
    assert result == RuleResult.allow()
