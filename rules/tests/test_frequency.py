"""Tests for the history-counting canon rules.

Three things are pinned here.

The **boundaries**, because a frequency limit is only as good as its edges: a booking one second
before a week starts belongs to the previous week, one starting exactly on the boundary instant
belongs to this one, and December does not spill into January. Each of those could plausibly have
gone the other way, and getting one wrong refuses a booking the user can see is legal.

**That ``week_starts_on`` is actually consulted.** A test whose result is the same for MONDAY and
SUNDAY has not tested it — the rule would pass just as well with the parameter ignored. The two
bookings below sit on the Sunday either side of the request precisely so that each falls in the
week under one setting and outside it under the other.

The **copy**, because denial text crosses into the UI verbatim. Expected strings are written out in
full rather than built from the helpers the rules use; deriving them would assert only that the code
agrees with itself.
"""

from datetime import datetime, timedelta, timezone

import pytest

from rules.frequency import MaxBookingsPerMonthRule, MaxBookingsPerWeekRule
from rules.interfaces import (
    BookingRecord,
    BookingRequest,
    CalendarContext,
    Context,
    HistoryContext,
    UserContext,
    Weekday,
)

USER = "u1"
RESOURCE = "court-1"

#: A Wednesday. The week containing it runs Mon 13th–Sun 19th when weeks start on Monday, and
#: Sun 12th–Sat 18th when they start on Sunday — which is what the two settings are told apart by.
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def at(moment: datetime) -> BookingRecord:
    """An hour-long existing booking starting at ``moment``."""
    return BookingRecord(
        user_id=USER, resource_id=RESOURCE, start_at=moment, end_at=moment + timedelta(hours=1)
    )


def request_at(moment: datetime) -> BookingRequest:
    return BookingRequest(
        user_id=USER, resource_id=RESOURCE, start_at=moment, end_at=moment + timedelta(hours=1)
    )


def context(
    *bookings: BookingRecord,
    now: datetime = NOW,
    week_starts_on: Weekday = Weekday.MONDAY,
) -> Context:
    return Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=week_starts_on, now=now),
        history=HistoryContext(bookings=bookings),
    )


# --- weekly ---------------------------------------------------------------------------------


def test_empty_history_passes():
    result = MaxBookingsPerWeekRule(2).evaluate(request_at(NOW), context())
    assert result.passed
    assert result.fail_reason is None


def test_nth_booking_passes_and_n_plus_first_fails():
    """The bound counts the request itself, so with a limit of 2 the *third* booking is refused."""
    rule = MaxBookingsPerWeekRule(2)
    one_existing = context(at(NOW - timedelta(days=1)))
    two_existing = context(at(NOW - timedelta(days=1)), at(NOW - timedelta(days=2)))

    assert rule.evaluate(request_at(NOW), one_existing).passed
    assert not rule.evaluate(request_at(NOW), two_existing).passed


def test_booking_just_before_the_week_boundary_does_not_count():
    """Mon 13th 00:00 starts the week; a booking a second earlier belongs to the previous one."""
    rule = MaxBookingsPerWeekRule(1)
    week_start = datetime(2026, 7, 13, tzinfo=timezone.utc)

    before = context(at(week_start - timedelta(seconds=1)))
    on_boundary = context(at(week_start))

    assert rule.evaluate(request_at(NOW), before).passed
    assert not rule.evaluate(request_at(NOW), on_boundary).passed


@pytest.mark.parametrize(
    "booking_day, denied_under, allowed_under",
    [
        # Sun 12th opens a Sunday-start week and precedes a Monday-start one.
        (datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc), Weekday.SUNDAY, Weekday.MONDAY),
        # Sun 19th closes a Monday-start week and follows a Sunday-start one.
        (datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc), Weekday.MONDAY, Weekday.SUNDAY),
    ],
)
def test_week_starts_on_decides_which_week_a_booking_falls_in(
    booking_day, denied_under, allowed_under
):
    """One booking, one request, **opposite verdicts** depending on where the week starts.

    Asserting opposite verdicts is the whole point. Two bookings straddling the request would leave
    each setting counting exactly one, and a rule that ignored ``week_starts_on`` entirely would
    agree with both — passing a test that had proved nothing.
    """
    rule = MaxBookingsPerWeekRule(1)
    booking = at(booking_day)

    denied = rule.evaluate(request_at(NOW), context(booking, week_starts_on=denied_under))
    allowed = rule.evaluate(request_at(NOW), context(booking, week_starts_on=allowed_under))

    assert not denied.passed
    assert allowed.passed


def test_weekly_denial_copy():
    rule = MaxBookingsPerWeekRule(2)
    full = context(at(NOW - timedelta(days=1)), at(NOW - timedelta(days=2)))

    result = rule.evaluate(request_at(NOW), full)

    assert result.fail_reason == (
        "You can make at most 2 bookings a week,"
        " and you already have 2 bookings that week."
        " Please pick a time in another week."
    )


def test_weekly_denial_copy_is_singular_at_one():
    result = MaxBookingsPerWeekRule(1).evaluate(
        request_at(NOW), context(at(NOW - timedelta(days=1)))
    )

    assert result.fail_reason == (
        "You can make at most 1 booking a week,"
        " and you already have 1 booking that week."
        " Please pick a time in another week."
    )


@pytest.mark.parametrize("limit", [0, -1])
def test_weekly_rule_rejects_a_non_positive_limit(limit):
    with pytest.raises(ValueError):
        MaxBookingsPerWeekRule(limit)


# --- monthly --------------------------------------------------------------------------------

#: Late December, so a January request and the December bookings either side of the rollover all
#: sit inside the history window the engine promises.
DECEMBER_NOW = datetime(2026, 12, 28, 10, 0, tzinfo=timezone.utc)


def test_monthly_empty_history_passes():
    assert MaxBookingsPerMonthRule(2).evaluate(request_at(NOW), context()).passed


def test_monthly_nth_passes_and_n_plus_first_fails():
    rule = MaxBookingsPerMonthRule(2)
    one = context(at(datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)))
    two = context(
        at(datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)),
        at(datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)),
    )

    assert rule.evaluate(request_at(NOW), one).passed
    assert not rule.evaluate(request_at(NOW), two).passed


def test_december_does_not_count_toward_january():
    """A year rollover is an ordinary month boundary, and the arithmetic must not wrap the year."""
    rule = MaxBookingsPerMonthRule(1)
    january_request = request_at(datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc))
    december_booking = context(
        at(datetime(2026, 12, 30, 9, 0, tzinfo=timezone.utc)), now=DECEMBER_NOW
    )

    assert rule.evaluate(january_request, december_booking).passed


def test_monthly_boundary_is_the_first_instant_of_the_month():
    rule = MaxBookingsPerMonthRule(1)
    january_request = request_at(datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc))
    january_start = datetime(2027, 1, 1, tzinfo=timezone.utc)

    just_before = context(at(january_start - timedelta(seconds=1)), now=DECEMBER_NOW)
    exactly_on = context(at(january_start), now=DECEMBER_NOW)

    assert rule.evaluate(january_request, just_before).passed
    assert not rule.evaluate(january_request, exactly_on).passed


def test_monthly_denial_copy():
    result = MaxBookingsPerMonthRule(1).evaluate(
        request_at(NOW), context(at(datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)))
    )

    assert result.fail_reason == (
        "You can make at most 1 booking a month,"
        " and you already have 1 booking that month."
        " Please pick a time in another month."
    )


@pytest.mark.parametrize("limit", [0, -1])
def test_monthly_rule_rejects_a_non_positive_limit(limit):
    with pytest.raises(ValueError):
        MaxBookingsPerMonthRule(limit)


# --- shared -------------------------------------------------------------------------------


def test_history_is_counted_regardless_of_what_it_describes():
    """Everything in HistoryContext counts. There is no status to inspect and none is inferred.

    The engine is deliberately ignorant of a schema that will keep changing; a booking that should
    not count toward a limit is one the caller does not put in the context.
    """
    rule = MaxBookingsPerWeekRule(1)
    assert not rule.evaluate(request_at(NOW), context(at(NOW - timedelta(days=1)))).passed
