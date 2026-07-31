"""Tests for the history-counting canon rules.

Three things are pinned here.

The **boundaries**, because a frequency limit is only as good as its edges: a booking one second
before a week starts belongs to the previous week, one starting exactly on the boundary instant
belongs to this one, and December does not spill into January. Each of those could plausibly have
gone the other way, and getting one wrong refuses a booking the user can see is legal.

**That the supplied window is actually consulted.** A test whose result is the same for two
different windows has not tested it — the rule would pass just as well ignoring the window. The two
bookings below sit on the Sunday either side of the request precisely so that each falls inside one
window and outside the other.

Resolving a timezone and a first-day-of-week convention into that pair of instants is the *caller's*
job since task 5.12, so it is tested where it happens — ``app/backend/tests/test_rules_stub.py``,
against real venues in Sydney, Honolulu and Berlin. What is pinned here is only that the rule obeys
the window it is given.

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


def utc_week(
    moment: datetime, week_starts_on: Weekday = Weekday.MONDAY
) -> tuple[datetime, datetime]:
    """The UTC-midnight week containing ``moment`` — what a UTC venue's window resolves to.

    These rules no longer derive their own window (see the module under test), so the tests below
    supply one. Computing it here keeps every boundary assertion saying what it always said, and
    keeps this file testing the *rule* rather than the resolution: which instants bound "the week
    this booking is in" is the adapter's decision and is tested against real timezones in
    ``app/backend/tests/test_rules_stub.py``.
    """
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    start = day - timedelta(days=(moment.weekday() - int(week_starts_on)) % 7)
    return start, start + timedelta(days=7)


def utc_month(moment: datetime) -> tuple[datetime, datetime]:
    """The UTC-midnight calendar month containing ``moment``. Companion to :func:`utc_week`."""
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def weekly(max_bookings: int, moment: datetime = NOW, week_starts_on: Weekday = Weekday.MONDAY):
    start, end = utc_week(moment, week_starts_on)
    return MaxBookingsPerWeekRule(max_bookings, window_start=start, window_end=end)


def monthly(max_bookings: int, moment: datetime = NOW):
    start, end = utc_month(moment)
    return MaxBookingsPerMonthRule(max_bookings, window_start=start, window_end=end)


# --- weekly ---------------------------------------------------------------------------------


def test_empty_history_passes():
    result = weekly(2).evaluate(request_at(NOW), context())
    assert result.passed
    assert result.fail_reason is None


def test_nth_booking_passes_and_n_plus_first_fails():
    """The bound counts the request itself, so with a limit of 2 the *third* booking is refused."""
    rule = weekly(2)
    one_existing = context(at(NOW - timedelta(days=1)))
    two_existing = context(at(NOW - timedelta(days=1)), at(NOW - timedelta(days=2)))

    assert rule.evaluate(request_at(NOW), one_existing).passed
    assert not rule.evaluate(request_at(NOW), two_existing).passed


def test_booking_just_before_the_week_boundary_does_not_count():
    """Mon 13th 00:00 starts the week; a booking a second earlier belongs to the previous one."""
    rule = weekly(1)
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
def test_the_supplied_window_decides_which_week_a_booking_falls_in(
    booking_day, denied_under, allowed_under
):
    """One booking, one request, **opposite verdicts** depending on the window supplied.

    Asserting opposite verdicts is the whole point. Two bookings straddling the request would leave
    each window counting exactly one, and a rule that ignored its window entirely would agree with
    both — passing a test that had proved nothing.

    The two windows here are the Monday-start and Sunday-start weeks around ``NOW``. The rule no
    longer reads ``week_starts_on`` — resolving a convention and a timezone into a pair of instants
    is the caller's job — so the parameter names below describe which *window*, not which context.
    """
    booking = at(booking_day)

    denied = weekly(1, week_starts_on=denied_under).evaluate(request_at(NOW), context(booking))
    allowed = weekly(1, week_starts_on=allowed_under).evaluate(request_at(NOW), context(booking))

    assert not denied.passed
    assert allowed.passed


def test_weekly_denial_copy():
    rule = weekly(2)
    full = context(at(NOW - timedelta(days=1)), at(NOW - timedelta(days=2)))

    result = rule.evaluate(request_at(NOW), full)

    assert result.fail_reason == (
        "You can make at most 2 bookings a week,"
        " and you already have 2 bookings that week."
        " Please pick a time in another week."
    )


def test_weekly_denial_copy_is_singular_at_one():
    result = weekly(1).evaluate(request_at(NOW), context(at(NOW - timedelta(days=1))))

    assert result.fail_reason == (
        "You can make at most 1 booking a week,"
        " and you already have 1 booking that week."
        " Please pick a time in another week."
    )


@pytest.mark.parametrize("limit", [0, -1])
def test_weekly_rule_rejects_a_non_positive_limit(limit):
    with pytest.raises(ValueError):
        MaxBookingsPerWeekRule(limit, *utc_week(NOW))


# --- monthly --------------------------------------------------------------------------------

#: Late December, so a January request and the December bookings either side of the rollover all
#: sit inside the history window the engine promises.
DECEMBER_NOW = datetime(2026, 12, 28, 10, 0, tzinfo=timezone.utc)


def test_monthly_empty_history_passes():
    assert monthly(2).evaluate(request_at(NOW), context()).passed


def test_monthly_nth_passes_and_n_plus_first_fails():
    rule = monthly(2)
    one = context(at(datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)))
    two = context(
        at(datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)),
        at(datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)),
    )

    assert rule.evaluate(request_at(NOW), one).passed
    assert not rule.evaluate(request_at(NOW), two).passed


def test_december_does_not_count_toward_january():
    """A year rollover is an ordinary month boundary, and the arithmetic must not wrap the year.

    The window is built from the request's own month, which is what the caller does — the window
    follows the request rather than ``now``, so a January request is judged against January.
    """
    january_request = request_at(datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc))
    rule = monthly(1, january_request.start_at)
    december_booking = context(
        at(datetime(2026, 12, 30, 9, 0, tzinfo=timezone.utc)), now=DECEMBER_NOW
    )

    assert rule.evaluate(january_request, december_booking).passed


def test_monthly_boundary_is_the_first_instant_of_the_month():
    january_request = request_at(datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc))
    rule = monthly(1, january_request.start_at)
    january_start = datetime(2027, 1, 1, tzinfo=timezone.utc)

    just_before = context(at(january_start - timedelta(seconds=1)), now=DECEMBER_NOW)
    exactly_on = context(at(january_start), now=DECEMBER_NOW)

    assert rule.evaluate(january_request, just_before).passed
    assert not rule.evaluate(january_request, exactly_on).passed


def test_monthly_denial_copy():
    result = monthly(1).evaluate(
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
        MaxBookingsPerMonthRule(limit, *utc_month(NOW))


# --- shared -------------------------------------------------------------------------------


def test_history_is_counted_regardless_of_what_it_describes():
    """Everything in HistoryContext counts. There is no status to inspect and none is inferred.

    The engine is deliberately ignorant of a schema that will keep changing; a booking that should
    not count toward a limit is one the caller does not put in the context.
    """
    rule = weekly(1)
    assert not rule.evaluate(request_at(NOW), context(at(NOW - timedelta(days=1)))).passed


# --- the window itself ------------------------------------------------------------------------


@pytest.mark.parametrize("rule_type", [MaxBookingsPerWeekRule, MaxBookingsPerMonthRule])
@pytest.mark.parametrize(
    "start, end",
    [
        # Naive: the caller forgot to convert at all.
        (datetime(2026, 7, 13), datetime(2026, 7, 20)),
        # Aware, wrong offset: the caller resolved a local window and passed it through unconverted,
        # which is the specific mistake this rule's new signature makes possible.
        (
            datetime(2026, 7, 13, tzinfo=timezone(timedelta(hours=10))),
            datetime(2026, 7, 20, tzinfo=timezone(timedelta(hours=10))),
        ),
    ],
)
def test_a_window_that_is_not_utc_is_refused(rule_type, start, end):
    with pytest.raises(ValueError):
        rule_type(1, window_start=start, window_end=end)


@pytest.mark.parametrize("rule_type", [MaxBookingsPerWeekRule, MaxBookingsPerMonthRule])
def test_an_inverted_or_empty_window_is_refused(rule_type):
    """Unlike availability hours, an inverted window here means nothing and is a caller bug.

    ``AvailabilityHoursRule`` reads an inverted pair as "this window crosses a UTC day" (task 5.13),
    so it is worth saying explicitly that these two rules do not: a counting window is a pair of
    absolute instants, not a recurring daily one, and there is no wrap for it to describe.
    """
    instant = datetime(2026, 7, 13, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        rule_type(1, window_start=instant, window_end=instant - timedelta(days=1))
    with pytest.raises(ValueError):
        rule_type(1, window_start=instant, window_end=instant)
