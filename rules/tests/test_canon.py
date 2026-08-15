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
    AvailabilityHoursRule,
    BookingHorizonRule,
    MaxConsecutiveDurationRule,
    MaxDurationRule,
    NotInThePastRule,
    SessionLengthRule,
    default_canon,
)
from rules.controller import evaluate_request
from rules.interfaces import (
    BookingRequest,
    CalendarContext,
    Context,
    LocalFrame,
    RuleResult,
    RunContext,
    UserContext,
    Weekday,
)
from tests.frames import solo_run, utc_frame

USER = "u1"
RESOURCE = "court-1"

#: Mid-morning, so a request built from it is comfortably inside default availability hours and a
#: denial can only have come from the rule under test.
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
    whole canon against a booking on another day says which day that is. ``AvailabilityHoursRule``
    reads the frame directly, so its tests must pass ``frame_end`` too: ``utc_frame`` otherwise
    defaults the frame's end to exactly one hour past ``frame_for``, which silently disagrees with
    a request built with a different duration.

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


# --- SessionLengthRule ------------------------------------------------------------------
#
# Replaces both `MinDurationRule` and `SlotAlignmentRule`
# (`ops/pending/bugs/grid-from-hours-and-min-duration.md`): a booking whose start and end both land
# on the grid is already at least one session long, so the floor those two rules used to enforce
# together falls out for free.


def session_rule(session_minutes: int = 30, anchor_minutes: int = 0) -> SessionLengthRule:
    return SessionLengthRule(session_minutes=session_minutes, anchor_minutes=anchor_minutes)


def test_a_start_and_end_both_on_the_grid_is_allowed():
    assert (
        session_rule()
        .evaluate(
            request(at(10, 0), at(10, 30)), context(frame_for=at(10, 0), frame_end=at(10, 30))
        )
        .passed
    )


def test_an_off_grid_start_is_denied():
    result = session_rule().evaluate(
        request(at(10, 7), at(10, 30)), context(frame_for=at(10, 7), frame_end=at(10, 30))
    )
    assert not result.passed
    assert "30-minute sessions" in (result.fail_reason or "")
    assert "does not start on a session boundary" in (result.fail_reason or "")


def test_an_off_grid_end_with_an_otherwise_aligned_start_is_denied():
    """Both bounds are checked independently: an aligned start does not excuse an off-grid end."""
    result = session_rule().evaluate(
        request(at(10, 0), at(10, 22)), context(frame_for=at(10, 0), frame_end=at(10, 22))
    )
    assert not result.passed
    assert "does not end on a session boundary" in (result.fail_reason or "")


def test_a_booking_starting_and_ending_several_sessions_later_is_allowed():
    """The exact boundary case: both bounds fall on session lines several sessions apart, not just
    one."""
    assert (
        session_rule()
        .evaluate(
            request(at(10, 0), at(11, 30)), context(frame_for=at(10, 0), frame_end=at(11, 30))
        )
        .passed
    )


def test_the_denial_copy_names_the_session_length():
    result = session_rule(session_minutes=45).evaluate(
        request(at(10, 0), at(10, 20)), context(frame_for=at(10, 0), frame_end=at(10, 20))
    )
    assert not result.passed
    assert "45-minute sessions" in (result.fail_reason or "")


def test_a_session_minutes_that_does_not_divide_a_day_is_rejected_at_construction():
    """1440 must be a whole number of sessions; 7 does not divide it."""
    with pytest.raises(ValueError):
        SessionLengthRule(session_minutes=7, anchor_minutes=0)


def test_a_non_positive_session_minutes_is_rejected_at_construction():
    with pytest.raises(ValueError):
        SessionLengthRule(session_minutes=0, anchor_minutes=0)


def test_an_anchor_minutes_outside_a_single_local_day_is_rejected_at_construction():
    with pytest.raises(ValueError):
        SessionLengthRule(session_minutes=30, anchor_minutes=1440)
    with pytest.raises(ValueError):
        SessionLengthRule(session_minutes=30, anchor_minutes=-1)


def test_the_grid_is_anchored_on_opening_time_not_on_local_midnight():
    """A non-midnight anchor still defines a valid grid — the rule never assumes its anchor is
    minute 0, which is the whole point of replacing `SlotAlignmentRule` (always anchored on local
    midnight) with a rule anchored on the venue's own opening time."""
    rule = session_rule(session_minutes=30, anchor_minutes=15)

    assert rule.evaluate(
        request(at(10, 15), at(10, 45)), context(frame_for=at(10, 15), frame_end=at(10, 45))
    ).passed
    assert not rule.evaluate(
        request(at(10, 0), at(10, 30)), context(frame_for=at(10, 0), frame_end=at(10, 30))
    ).passed


def test_a_start_before_the_anchor_still_lands_on_the_extended_grid():
    """Python's `%` is non-negative for a negative left operand, so a booking that starts before
    the venue's own opening time still lines up with the grid if it is congruent to the anchor
    modulo the session length — the rule never treats "before the anchor" as a special case."""
    rule = session_rule(session_minutes=60, anchor_minutes=9 * 60)  # opens at 09:00

    on_grid = request(at(8, 0), at(9, 0))
    assert rule.evaluate(on_grid, context(frame_for=at(8, 0), frame_end=at(9, 0))).passed

    off_grid = request(at(8, 15), at(9, 0))
    assert not rule.evaluate(off_grid, context(frame_for=at(8, 15), frame_end=at(9, 0))).passed


def test_the_rule_reads_only_the_local_frame_never_the_requests_own_utc_clock():
    """A venue off UTC resolves a local frame that can disagree with the request's raw UTC clock —
    the whole reason this rule, like `AvailabilityHoursRule`, reads `context.local` and never
    touches `request.start_at` / `request.end_at` directly. The request below has a UTC start
    (13:07) that is off a 30-minute grid; a rule reading it directly would deny. The local frame
    handed alongside it — as an adapter for a venue comfortably off UTC might resolve it — reports
    local midnight instead, which is on the grid, and that is what the rule allows against."""
    rule = session_rule(session_minutes=30, anchor_minutes=0)

    off_grid_utc_request = request(
        datetime(2026, 7, 20, 13, 7, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 13, 37, tzinfo=timezone.utc),
    )
    local_frame = LocalFrame(
        day_start=datetime(2026, 7, 21, tzinfo=timezone.utc),
        day_end=datetime(2026, 7, 22, tzinfo=timezone.utc),
        week_start=datetime(2026, 7, 20, tzinfo=timezone.utc),
        week_end=datetime(2026, 7, 27, tzinfo=timezone.utc),
        month_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        month_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
        weekday=1,
        start_minutes=0,
        end_minutes=30,
    )
    ctx = Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=NOW),
        local=local_frame,
        run=RunContext(
            start_at=off_grid_utc_request.start_at,
            end_at=off_grid_utc_request.end_at,
            booking_count=1,
        ),
    )
    assert rule.evaluate(off_grid_utc_request, ctx).passed


# --- AvailabilityHoursRule ----------------------------------------------------------
#
# `opens_at_minutes` / `closes_at_minutes` are minutes from the venue's own **local** midnight
# (`rules/rules/registry.py`, `.claude/rules/rule-engine.md`) — there is no UTC clock time and no
# timezone anywhere in this rule any more. Every test below builds its frame from the request's own
# start and end (`hours_context`) rather than relying on the module's default `NOW`-anchored
# `context()`: this rule reads `context.local.start_minutes` / `end_minutes` exclusively, so a
# frame resolved for the wrong day or the wrong duration would silently test the wrong scenario.


def hours_rule() -> AvailabilityHoursRule:
    return AvailabilityHoursRule(opens_at_minutes=6 * 60, closes_at_minutes=23 * 60)


def hours_context(start_at: datetime, end_at: datetime) -> Context:
    return context(
        frame_for=start_at,
        frame_end=end_at,
        run=RunContext(start_at=start_at, end_at=end_at, booking_count=1),
    )


def test_a_booking_starting_exactly_at_opening_is_allowed():
    start, end = at(6, 0), at(7, 0)
    assert hours_rule().evaluate(request(start, end), hours_context(start, end)).passed


def test_a_booking_starting_a_minute_before_opening_is_denied():
    start, end = at(5, 59), at(6, 59)
    result = hours_rule().evaluate(request(start, end), hours_context(start, end))
    assert not result.passed
    assert result.fail_reason == (
        "That's before we open, so this booking starts too early."
        " Please check this Space's opening hours on the calendar and pick a later time."
    )


def test_a_booking_ending_exactly_at_closing_is_allowed():
    """The closing bound is inclusive."""
    start, end = at(22, 0), at(23, 0)
    assert hours_rule().evaluate(request(start, end), hours_context(start, end)).passed


def test_a_booking_ending_a_minute_after_closing_is_denied():
    start, end = at(22, 0), at(23, 1)
    result = hours_rule().evaluate(request(start, end), hours_context(start, end))
    assert not result.passed
    assert result.fail_reason == (
        "That's after we close, so this booking runs too late."
        " Please check this Space's opening hours on the calendar and pick an earlier time."
    )


def test_a_booking_running_past_local_midnight_is_denied_not_wrapped():
    """The frame is resolved for the request's own **start** date, so a booking ending after local
    midnight reports `end_minutes > 1440` rather than wrapping back into an early-morning reading.
    """
    start, end = at(23, 0, day=20), at(0, 30, day=21)
    result = hours_rule().evaluate(request(start, end), hours_context(start, end))
    assert not result.passed
    assert "runs too late" in (result.fail_reason or "")


def test_equal_opening_and_closing_is_rejected_at_construction():
    with pytest.raises(ValueError):
        AvailabilityHoursRule(opens_at_minutes=6 * 60, closes_at_minutes=6 * 60)


def test_opens_at_minutes_out_of_a_single_day_is_rejected_at_construction():
    with pytest.raises(ValueError):
        AvailabilityHoursRule(opens_at_minutes=1440, closes_at_minutes=1500)
    with pytest.raises(ValueError):
        AvailabilityHoursRule(opens_at_minutes=-1, closes_at_minutes=60)


def test_a_window_longer_than_24_hours_is_rejected_at_construction():
    with pytest.raises(ValueError):
        AvailabilityHoursRule(opens_at_minutes=6 * 60, closes_at_minutes=6 * 60 + 1441)


def test_a_window_open_the_full_24_hours_has_no_gap():
    """``closes_at_minutes == opens_at_minutes + 1440`` is the legal upper bound: always open."""
    rule = AvailabilityHoursRule(opens_at_minutes=0, closes_at_minutes=1440)
    start, end = at(3, 0), at(3, 30)
    assert rule.evaluate(request(start, end), hours_context(start, end)).passed


# --- AvailabilityHoursRule, crossing local midnight ----------------------------------
#
# `closes_at_minutes > 1440` is not a construction error: it is how a venue open past its own
# local midnight (`ops/done/stream-7/passed-midnight.md`) is represented — 18:00 to 02:00 is
# `opens_at_minutes=1080, closes_at_minutes=1560`, plainly. There is no UTC calendar day to cross
# any more; this is the venue's own local calendar day, full stop.


def wrapping_hours_rule() -> AvailabilityHoursRule:
    """Opens at 23:00 local, closes at 11:00 local the following day — a crossing window."""
    return AvailabilityHoursRule(opens_at_minutes=23 * 60, closes_at_minutes=11 * 60 + 1440)


def test_a_crossing_window_constructs_without_error():
    rule = wrapping_hours_rule()
    assert rule.opens_at_minutes == 23 * 60
    assert rule.closes_at_minutes == 11 * 60 + 1440


def test_a_crossing_window_allows_a_booking_shortly_after_opening():
    """23:30 on day 20 is the "opens today, closes tomorrow" half of one occurrence."""
    start, end = at(23, 30, day=20), at(0, 30, day=21)
    assert wrapping_hours_rule().evaluate(request(start, end), hours_context(start, end)).passed


def test_a_crossing_window_allows_a_booking_shortly_before_closing():
    """10:00-11:00 on day 21 is the "opened yesterday, closes today" half of the same occurrence."""
    start, end = at(10, 0, day=21), at(11, 0, day=21)
    assert wrapping_hours_rule().evaluate(request(start, end), hours_context(start, end)).passed


def test_a_crossing_window_denies_a_booking_in_the_daily_gap():
    """15:00 sits strictly between closing (11:00) and the next opening (23:00) — never in hours.

    This is the case that used to raise `MidnightWrapError` and deny every booking on the Space with
    the engine's generic copy, regardless of when it actually fell (`ops/done/stream-7/
    passed-midnight.md`). Denied here for the specific, actionable reason.
    """
    start, end = at(15, 0, day=20), at(16, 0, day=20)
    result = wrapping_hours_rule().evaluate(request(start, end), hours_context(start, end))
    assert not result.passed
    assert "starts too early" in (result.fail_reason or "")


def test_a_crossing_window_denies_a_booking_that_runs_past_the_wrapped_close():
    start, end = at(10, 30, day=21), at(11, 30, day=21)
    result = wrapping_hours_rule().evaluate(request(start, end), hours_context(start, end))
    assert not result.passed
    assert "runs too late" in (result.fail_reason or "")


def test_a_crossing_window_closing_bound_is_inclusive():
    """Ending exactly at the wrapped closing instant is fine, one minute later is not."""
    start = at(10, 30, day=21)
    on_close, off_close = at(11, 0, day=21), at(11, 1, day=21)

    assert (
        wrapping_hours_rule()
        .evaluate(request(start, on_close), hours_context(start, on_close))
        .passed
    )
    assert not (
        wrapping_hours_rule()
        .evaluate(request(start, off_close), hours_context(start, off_close))
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
    req = hours_from(far, hours=3)
    result = evaluate_request(req, context(frame_for=far, run=solo_run(req)), DEFAULT_CANON)
    assert "days ahead" in (result.fail_reason or "")


def test_a_past_denial_beats_a_duration_denial():
    yesterday = NOW - timedelta(days=1)
    req = hours_from(yesterday, hours=3)
    result = evaluate_request(req, context(frame_for=yesterday, run=solo_run(req)), DEFAULT_CANON)
    assert "already passed" in (result.fail_reason or "")


def test_duration_is_reported_before_availability_hours():
    """An over-long booking that also runs past closing reports its length first."""
    req = request(at(21, 30), at(23, 45))
    result = evaluate_request(req, context(frame_for=at(21, 30), run=solo_run(req)), DEFAULT_CANON)
    assert "at most 2 hours" in (result.fail_reason or "")


def test_an_ordinary_booking_passes_the_whole_canon():
    tomorrow = NOW + timedelta(days=1)
    req = hours_from(tomorrow)
    result = evaluate_request(req, context(frame_for=tomorrow, run=solo_run(req)), DEFAULT_CANON)
    assert result == RuleResult.allow()
