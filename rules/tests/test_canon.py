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

from datetime import datetime, timedelta, timezone

import pytest

from rules.canon import (
    DEFAULT_CANON,
    BookingHorizonRule,
    MaxConsecutiveDurationRule,
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
    RunContext,
    UserContext,
    Weekday,
)
from tests.frames import solo_run, utc_frame

USER = "u1"
RESOURCE = "court-1"

#: Mid-morning, comfortably clear of midnight in either direction, so a denial can only have come
#: from the rule under test.
NOW = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


def request(start_at: datetime, end_at: datetime) -> BookingRequest:
    return BookingRequest(user_id=USER, resource_id=RESOURCE, start_at=start_at, end_at=end_at)


def hours_from(moment: datetime, hours: float = 1) -> BookingRequest:
    return request(moment, moment + timedelta(hours=hours))


def context(
    now: datetime = NOW,
    *,
    frame_for: datetime | None = None,
    frame_end: datetime | None = None,
    run: RunContext | None = None,
) -> Context:
    """A context for a UTC venue, its local frame resolved for ``frame_for``'s own day.

    Most rules here read only the request and its own parameters, so the frame is inert for them
    — but ``evaluate_request`` cross-checks it against the request's start, so a test running the
    whole canon against a booking on another day says which day that is.

    No rule in this module reads ``context.run``, so ``run`` defaults to a span matching the same
    default width the frame gets — inert, like the frame is for these rules — and a caller running
    the *whole canon* through ``evaluate_request`` against a request of a different shape must pass
    its own ``run`` (``solo_run(that_request)``) or the controller's run cross-check raises for a
    reason unrelated to what the test is about.
    """
    start = frame_for if frame_for is not None else now
    end = frame_end if frame_end is not None else start + timedelta(hours=1)
    return Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=now),
        local=utc_frame(start, frame_end),
        run=run or RunContext(start_at=start, end_at=end, booking_count=1),
    )


def at(hour: int, minute: int = 0, day: int = 20) -> datetime:
    """A UTC instant on the reference day."""
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


# --- MaxConsecutiveDurationRule ---------------------------------------------------------
#
# The bug report itself (`ops/pending/bugs/max-duration-cannon.md`): a Space configures "max 2
# hours" meaning a session, a member books 17:00-18:00 and then, separately, 18:00-19:00, and
# `MaxDurationRule` passes both because each request's own span is one hour. This rule reads
# `context.run` instead of `request.duration`, so the case that matters is a *short* request that
# joins a run already over the cap — every case below builds its own `context.run` rather than
# relying on `context()`'s inert one-hour default, since that default would make this rule
# indistinguishable from `MaxDurationRule` in every test.


def joined_context(run_start: datetime, run_end: datetime, booking_count: int = 2) -> Context:
    return context(run=RunContext(start_at=run_start, end_at=run_end, booking_count=booking_count))


def test_a_request_that_joins_a_run_of_exactly_max_duration_is_allowed():
    """The bound is inclusive, the same convention every duration rule in this canon shares."""
    rule = MaxConsecutiveDurationRule(max_duration=timedelta(hours=2))
    one_hour_request = request(NOW, NOW + timedelta(hours=1))
    ctx = joined_context(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert rule.evaluate(one_hour_request, ctx) == RuleResult.allow()


def test_a_one_hour_request_that_joins_a_held_one_hour_booking_is_denied_by_a_two_hour_cap():
    """The bug report, as a test. Neither booking is itself over 2 hours — `MaxDurationRule` would
    pass both — but the run the second one joins comes to 2 hours and 1 minute, which this rule
    catches and `MaxDurationRule` structurally cannot."""
    rule = MaxConsecutiveDurationRule(max_duration=timedelta(hours=2))
    held = request(NOW, NOW + timedelta(hours=1))
    second = request(NOW + timedelta(hours=1), NOW + timedelta(hours=1, minutes=1))

    # The held booking alone: nothing to join, well under the cap.
    assert (
        rule.evaluate(held, joined_context(NOW, NOW + timedelta(hours=1), 1)) == RuleResult.allow()
    )

    # The second booking, joined to the first into one run one minute over the cap.
    result = rule.evaluate(second, joined_context(NOW, NOW + timedelta(hours=2, minutes=1)))
    assert not result.passed


def test_the_same_one_hour_request_with_no_neighbour_is_allowed():
    """The other half of the bug-report pair: the identical one-hour request, alone, passes — the
    rule denies what the run comes to, never the request's own length."""
    rule = MaxConsecutiveDurationRule(max_duration=timedelta(hours=2))
    solo_request = request(NOW, NOW + timedelta(hours=1))
    assert rule.evaluate(solo_request, context()) == RuleResult.allow()


def test_a_short_request_still_denies_when_it_pushes_the_run_over_the_cap():
    """A five-minute request is nowhere near 2 hours on its own — the property that makes this rule
    genuinely different from `MaxDurationRule`, which would allow this request outright."""
    rule = MaxConsecutiveDurationRule(max_duration=timedelta(hours=2))
    five_minutes = request(NOW, NOW + timedelta(minutes=5))
    ctx = joined_context(NOW - timedelta(hours=2), NOW + timedelta(minutes=5))
    assert not rule.evaluate(five_minutes, ctx).passed


def test_the_denial_copy_is_about_the_run_not_the_request():
    """A user submitting a one-hour booking must not be told "bookings can be at most 2 hours" —
    that reads as false, since one hour is under the cap. The copy has to make clear the refusal is
    about what the booking joins."""
    rule = MaxConsecutiveDurationRule(max_duration=timedelta(hours=2))
    one_hour_request = request(NOW, NOW + timedelta(hours=1))
    ctx = joined_context(NOW - timedelta(hours=1, minutes=30), NOW + timedelta(hours=1))
    result = rule.evaluate(one_hour_request, ctx)

    assert not result.passed
    assert result.fail_reason == (
        "Bookings can't add up to more than 2 hours of consecutive play back-to-back,"
        " and joining this one to what you already have booked next to it would come to"
        " 2 hours and 30 minutes."
        " Please leave a gap before or after it, or shorten it, and try again."
    )
    assert "at most 2 hours long" not in result.fail_reason
    assert "consecutive" in result.fail_reason


def test_a_non_positive_max_consecutive_duration_is_rejected_at_construction():
    with pytest.raises(ValueError):
        MaxConsecutiveDurationRule(max_duration=timedelta(0))


# --- The canon, in order ------------------------------------------------------------


def test_the_default_canon_is_the_three_rules_in_the_documented_order():
    assert [type(rule).__name__ for rule in DEFAULT_CANON] == [
        "NotInThePastRule",
        "BookingHorizonRule",
        "MaxDurationRule",
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
    req = hours_from(far, hours=3)
    result = evaluate_request(req, context(frame_for=far, run=solo_run(req)), DEFAULT_CANON)
    assert "days ahead" in (result.fail_reason or "")


def test_a_past_denial_beats_a_duration_denial():
    yesterday = NOW - timedelta(days=1)
    req = hours_from(yesterday, hours=3)
    result = evaluate_request(req, context(frame_for=yesterday, run=solo_run(req)), DEFAULT_CANON)
    assert "already passed" in (result.fail_reason or "")


def test_an_over_long_booking_denies_with_duration_copy_through_the_full_canon():
    """A booking too long for `MaxDurationRule` still denies with duration copy once opening hours
    are the calendar shape's own gate rather than a canon rule this booking could also break — the
    `AvailabilityHoursRule` ordering this test used to pin retired with that type
    (`.claude/rules/calendar-shape.md`, "Two rule types this document replaced")."""
    req = request(at(21, 30), at(23, 45))
    result = evaluate_request(req, context(frame_for=at(21, 30), run=solo_run(req)), DEFAULT_CANON)
    assert "at most 2 hours" in (result.fail_reason or "")


def test_an_ordinary_booking_passes_the_whole_canon():
    tomorrow = NOW + timedelta(days=1)
    req = hours_from(tomorrow)
    result = evaluate_request(req, context(frame_for=tomorrow, run=solo_run(req)), DEFAULT_CANON)
    assert result == RuleResult.allow()
