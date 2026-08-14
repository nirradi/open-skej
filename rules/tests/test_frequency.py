"""Tests for the history-counting canon rules.

Three things are pinned here.

The **boundaries**, because a frequency limit is only as good as its edges: a session one second
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

**What task 8.6 changed here.** These rules now merge ``request`` with ``context.history.bookings``
themselves, inside ``evaluate`` (``rules.spans.merge_adjoining_spans`` plus a ``tolerance`` supplied
at construction — see ``rules/rules/frequency.py``'s module docstring for why the merge lives in the
rule rather than being folded into ``Context.history`` ahead of time), and compare the merged count
to the cap directly, no ``+1``. The request is always one of the spans the sweep merges, so when
nothing in history is close enough to abut it, the merge reproduces the old ``existing + 1 >
max_bookings`` bound exactly — the request contributes its own run precisely where it used to
contribute a bare ``+1``. What is new is exactly the case where something *does* abut: the
request's own run then merges with an existing one instead of adding a second, which is the whole
point of this task and is covered separately below.

**What task 8.7 added.** ``MaxBookingsPerDayRule`` is a third counting rule, built and tested
identically to the week and month ones above — a "--- daily ---" section below mirrors the weekly
one rather than introducing a new pattern. ``MaxDurationPerDayRule`` is the other shape entirely: a
*total*, not a count, and the one rule in this module that does not call
``merge_adjoining_spans`` at all — its own section below pins that directly, including the
overlapping-Resource case its docstring gives as the reason.
"""

from datetime import datetime, timedelta, timezone

import pytest

from rules.frequency import (
    MaxBookingsPerDayRule,
    MaxBookingsPerMonthRule,
    MaxBookingsPerWeekRule,
    MaxDurationPerDayRule,
)
from rules.interfaces import (
    BookingRecord,
    BookingRequest,
    CalendarContext,
    Context,
    HistoryContext,
    RunContext,
    UserContext,
    Weekday,
)
from tests.frames import utc_frame

USER = "u1"
RESOURCE = "court-1"

#: A Wednesday. The week containing it runs Mon 13th–Sun 19th when weeks start on Monday, and
#: Sun 12th–Sat 18th when they start on Sunday — which is what the two settings are told apart by.
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def at(moment: datetime) -> BookingRecord:
    """An hour-long session already in history, starting at ``moment``."""
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
    # These rules are driven through `.evaluate(request, context)` directly, never through
    # `evaluate_request`'s cross-check (module docstring), so `run` has nothing it must stay
    # aligned with — a fixed one-booking span anchored on `now` is enough.
    return Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=week_starts_on, now=now),
        local=utc_frame(now, week_starts_on=week_starts_on),
        run=RunContext(start_at=now, end_at=now + timedelta(hours=1), booking_count=1),
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


def utc_day(moment: datetime) -> tuple[datetime, datetime]:
    """The UTC-midnight day containing ``moment`` (task 8.7). Companion to :func:`utc_week` /
    :func:`utc_month`.

    ``start + timedelta(days=1)`` is fine here, unlike in the adapter that resolves a *real* local
    day (``app.rules_stub._local_day_bounds``): this helper never crosses a DST transition because
    it stays on the UTC calendar, which has none. The DST case belongs to
    ``app/backend/tests/test_rules_stub.py``, against a real zone.
    """
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def utc_month(moment: datetime) -> tuple[datetime, datetime]:
    """The UTC-midnight calendar month containing ``moment``. Companion to :func:`utc_week`."""
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def weekly(
    max_bookings: int,
    moment: datetime = NOW,
    week_starts_on: Weekday = Weekday.MONDAY,
    tolerance: timedelta = timedelta(0),
):
    start, end = utc_week(moment, week_starts_on)
    return MaxBookingsPerWeekRule(
        max_bookings, window_start=start, window_end=end, tolerance=tolerance
    )


def monthly(max_bookings: int, moment: datetime = NOW, tolerance: timedelta = timedelta(0)):
    start, end = utc_month(moment)
    return MaxBookingsPerMonthRule(
        max_bookings, window_start=start, window_end=end, tolerance=tolerance
    )


def daily(max_bookings: int, moment: datetime = NOW, tolerance: timedelta = timedelta(0)):
    start, end = utc_day(moment)
    return MaxBookingsPerDayRule(
        max_bookings, window_start=start, window_end=end, tolerance=tolerance
    )


def daily_duration(max_duration: timedelta, moment: datetime = NOW):
    start, end = utc_day(moment)
    return MaxDurationPerDayRule(max_duration, window_start=start, window_end=end)


# --- weekly ---------------------------------------------------------------------------------


def test_empty_history_passes():
    result = weekly(2).evaluate(request_at(NOW), context())
    assert result.passed
    assert result.fail_reason is None


def test_at_the_cap_passes_and_one_more_denies():
    """The request is always one of the spans merged, so with nothing in history close enough to
    abut it, this reproduces the old "the bound counts the request itself" bound exactly."""
    rule = weekly(2)
    one_existing = context(at(NOW - timedelta(days=1)))
    two_existing = context(at(NOW - timedelta(days=1)), at(NOW - timedelta(days=2)))

    assert rule.evaluate(request_at(NOW), one_existing).passed
    assert not rule.evaluate(request_at(NOW), two_existing).passed


def test_booking_just_before_the_week_boundary_does_not_count():
    """Mon 13th 00:00 starts the week; a session a second earlier belongs to the previous one."""
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
        "You can make at most 2 sessions a week,"
        " and this booking would bring you to 3 sessions that week."
        " Please pick a time in another week."
    )


def test_weekly_denial_copy_is_singular_at_the_cap():
    """``_format_sessions`` renders ``max_bookings=1`` as "1 session" — the merged count that
    triggers a denial can never itself be singular, since denying requires it to exceed 1."""
    result = weekly(1).evaluate(request_at(NOW), context(at(NOW - timedelta(days=1))))

    assert result.fail_reason == (
        "You can make at most 1 session a week,"
        " and this booking would bring you to 2 sessions that week."
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


def test_monthly_at_the_cap_passes_and_one_more_denies():
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
        "You can make at most 1 session a month,"
        " and this booking would bring you to 2 sessions that month."
        " Please pick a time in another month."
    )


@pytest.mark.parametrize("limit", [0, -1])
def test_monthly_rule_rejects_a_non_positive_limit(limit):
    with pytest.raises(ValueError):
        MaxBookingsPerMonthRule(limit, *utc_month(NOW))


# --- daily (task 8.7) -------------------------------------------------------------------------
#
# Built exactly like the weekly rule above — same merge, same "no +1", same half-open window — so
# this section mirrors that one rather than inventing a new pattern.


def test_daily_empty_history_passes():
    result = daily(2).evaluate(request_at(NOW), context())
    assert result.passed
    assert result.fail_reason is None


def test_daily_at_the_cap_passes_and_one_more_denies():
    """The two existing sessions sit 9 and 7 hours before the request — far enough apart, and far
    enough from the request itself, that none of the three abut (unlike the ``hours=1`` gap
    ``test_a_request_extending_a_held_session_counts_once_not_twice_against_a_daily_cap`` uses
    deliberately below), so this is genuinely three separate sessions in one day."""
    rule = daily(2)
    one_existing = context(at(NOW - timedelta(hours=9)))
    two_existing = context(at(NOW - timedelta(hours=9)), at(NOW - timedelta(hours=7)))

    assert rule.evaluate(request_at(NOW), one_existing).passed
    assert not rule.evaluate(request_at(NOW), two_existing).passed


def test_booking_just_before_the_day_boundary_does_not_count():
    """Midnight starts the day; a session a second earlier belongs to the previous one."""
    rule = daily(1)
    day_start = datetime(2026, 7, 15, tzinfo=timezone.utc)

    before = context(at(day_start - timedelta(seconds=1)))
    on_boundary = context(at(day_start))

    assert rule.evaluate(request_at(NOW), before).passed
    assert not rule.evaluate(request_at(NOW), on_boundary).passed


def test_the_supplied_window_decides_which_day_a_booking_falls_in():
    """Mirrors the weekly/monthly versions above: the same booking and request, opposite verdicts
    depending on which day-window is supplied — proof the rule consults the window it is handed
    rather than deriving one from the request's own UTC date."""
    booking = at(NOW - timedelta(hours=2))  # 08:00, an hour clear of the request's own run

    midnight = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    window_covering_both = (midnight, midnight + timedelta(days=1))
    window_covering_only_the_request = (
        NOW - timedelta(minutes=30),
        NOW + timedelta(hours=23, minutes=30),
    )

    denied = MaxBookingsPerDayRule(1, *window_covering_both).evaluate(
        request_at(NOW), context(booking)
    )
    allowed = MaxBookingsPerDayRule(1, *window_covering_only_the_request).evaluate(
        request_at(NOW), context(booking)
    )

    assert not denied.passed
    assert allowed.passed


def test_daily_denial_copy():
    rule = daily(2)
    full = context(at(NOW - timedelta(hours=9)), at(NOW - timedelta(hours=7)))

    result = rule.evaluate(request_at(NOW), full)

    assert result.fail_reason == (
        "You can make at most 2 sessions a day,"
        " and this booking would bring you to 3 sessions that day."
        " Please pick another day."
    )


def test_daily_denial_copy_is_singular_at_the_cap():
    result = daily(1).evaluate(request_at(NOW), context(at(NOW - timedelta(hours=9))))

    assert result.fail_reason == (
        "You can make at most 1 session a day,"
        " and this booking would bring you to 2 sessions that day."
        " Please pick another day."
    )


def test_a_request_extending_a_held_session_counts_once_not_twice_against_a_daily_cap():
    """The task 8.6 regression, replayed for the day window: the request abuts the held booking
    exactly, so even zero tolerance joins them into one session."""
    rule = daily(1)
    held = at(NOW - timedelta(hours=1))  # ends exactly where the request starts

    result = rule.evaluate(request_at(NOW), context(held))

    assert result.passed


@pytest.mark.parametrize("limit", [0, -1])
def test_daily_rule_rejects_a_non_positive_limit(limit):
    with pytest.raises(ValueError):
        MaxBookingsPerDayRule(limit, *utc_day(NOW))


# --- max_duration_per_day (task 8.7) ----------------------------------------------------------
#
# The other shape: a total, not a count, and the one rule in this module that does not call
# ``merge_adjoining_spans`` — see ``MaxDurationPerDayRule``'s own docstring for why, and
# ``test_the_total_is_the_sum_not_the_merged_run_span`` below for the case that decision protects.


def test_duration_per_day_empty_history_passes():
    result = daily_duration(timedelta(hours=2)).evaluate(request_at(NOW), context())
    assert result.passed
    assert result.fail_reason is None


def test_duration_per_day_of_exactly_the_cap_passes_and_one_minute_more_denies():
    """The bound is inclusive, the same convention every duration rule in the canon shares."""
    exactly_at_cap = daily_duration(timedelta(hours=2)).evaluate(
        BookingRequest(
            user_id=USER, resource_id=RESOURCE, start_at=NOW, end_at=NOW + timedelta(hours=1)
        ),
        context(at(NOW - timedelta(hours=2))),  # a separate hour earlier the same day
    )
    one_minute_over = daily_duration(timedelta(hours=2)).evaluate(
        BookingRequest(
            user_id=USER,
            resource_id=RESOURCE,
            start_at=NOW,
            end_at=NOW + timedelta(hours=1, minutes=1),
        ),
        context(at(NOW - timedelta(hours=2))),
    )

    assert exactly_at_cap.passed
    assert not one_minute_over.passed


def test_duration_per_day_sums_every_history_entry_in_the_window():
    rule = daily_duration(timedelta(hours=2))
    total_two_hours = context(
        at(NOW - timedelta(hours=5)), at(NOW - timedelta(hours=3))
    )  # two separate one-hour entries earlier the same day

    result = rule.evaluate(request_at(NOW), total_two_hours)

    assert not result.passed


def test_duration_per_day_ignores_a_history_entry_starting_outside_the_window():
    """An entry that starts before the day's own window does not count toward its total, however
    long it runs — the window is on ``start_at`` alone, matching every other rule in this module."""
    rule = daily_duration(timedelta(minutes=90))
    day_start = datetime(2026, 7, 15, tzinfo=timezone.utc)
    the_previous_day = context(at(day_start - timedelta(minutes=1)))

    result = rule.evaluate(request_at(NOW), the_previous_day)

    assert result.passed


def test_duration_per_day_denial_copy():
    rule = daily_duration(timedelta(hours=2))
    # A one-hour entry earlier the same day plus a 90-minute request totals 2 hours 30 minutes.
    over = context(at(NOW - timedelta(hours=2)))

    result = rule.evaluate(
        BookingRequest(
            user_id=USER,
            resource_id=RESOURCE,
            start_at=NOW,
            end_at=NOW + timedelta(hours=1, minutes=30),
        ),
        over,
    )

    assert result.fail_reason == (
        "Bookings can add up to at most 2 hours a day,"
        " and this one would bring your total to 2 hours and 30 minutes today."
        " Please shorten it, or pick another day, and try again."
    )


@pytest.mark.parametrize("bad_duration", [timedelta(0), timedelta(minutes=-5)])
def test_duration_per_day_rule_rejects_a_non_positive_max_duration(bad_duration):
    with pytest.raises(ValueError):
        MaxDurationPerDayRule(bad_duration, *utc_day(NOW))


def test_the_total_is_the_sum_not_the_merged_run_span():
    """The case ``MaxDurationPerDayRule``'s own docstring exists to protect: a user holding two
    Resources at overlapping times.

    Two one-hour bookings on different courts, 09:00-10:00 and 09:30-10:30, overlap by half an
    hour. Summed as raw entries — what this rule does — they total **2 hours**. Merged into a run
    first — what every counting rule in this module does instead, and what this rule's docstring
    explains *not* doing — they collapse to a single 09:00-10:30 span, **90 minutes**, because
    ``merge_adjoining_spans`` treats an overlap (a negative gap) exactly like an abutment. A request
    elsewhere the same day adds a further 15 minutes on either reading.

    The cap below (110 minutes) sits strictly between the two: the correct raw-sum total is 135
    minutes, over the cap, so a correctly-implemented rule denies; a rule that merged first would
    compute 105 minutes, under the cap, and silently pass a booking it should have refused — the
    exact under-count the docstring warns about. Pinning the denial (not only the exact total in
    the copy) is what makes this test fail if a future edit reintroduces the merge.
    """
    rule = daily_duration(timedelta(minutes=110))
    overlapping_history = (
        BookingRecord(
            user_id=USER, resource_id="court-1", start_at=NOW - timedelta(hours=1), end_at=NOW
        ),
        BookingRecord(
            user_id=USER,
            resource_id="court-2",
            start_at=NOW - timedelta(minutes=30),
            end_at=NOW + timedelta(minutes=30),
        ),
    )
    # Far from both history entries, so it never merges with them under any tolerance — its own 15
    # minutes lands on top of either reading unchanged, keeping the two totals cleanly 30 minutes
    # apart (135 vs. 105) regardless of merge behaviour.
    request = BookingRequest(
        user_id=USER,
        resource_id=RESOURCE,
        start_at=NOW + timedelta(hours=2),
        end_at=NOW + timedelta(hours=2, minutes=15),
    )

    result = rule.evaluate(request, context(*overlapping_history))

    assert not result.passed
    assert result.fail_reason == (
        "Bookings can add up to at most 1 hour and 50 minutes a day,"
        " and this one would bring your total to 2 hours and 15 minutes today."
        " Please shorten it, or pick another day, and try again."
    )


# --- the merge itself (task 8.6) --------------------------------------------------------------


def test_a_request_extending_a_held_session_counts_once_not_twice():
    """The regression task 8.6 exists for. With no merging this would be one existing row plus the
    request, two against a cap of one — refused. Merging them (the request abuts the held booking
    exactly, so even zero tolerance joins them) makes it one session instead."""
    rule = weekly(1)
    held = at(NOW - timedelta(hours=1))  # ends exactly where the request starts

    result = rule.evaluate(request_at(NOW), context(held))

    assert result.passed


def test_a_single_merged_session_counts_once_however_many_rows_it_was_built_from():
    """This rule has no idea whether one ``BookingRecord`` in history is a single short booking or
    the merged result of several rows abutting each other — merging happens once, on ``request``
    and every history entry together, and the rule counts whatever runs fall out of it.

    That is exactly the lever ``app.rules_stub`` pulls in production: a two-hour session built from
    two adjoining one-hour bookings merges to one run, which is what makes it consume one slot of a
    cap rather than two. Contrasted here against two *separate* one-hour bookings covering the same
    total time, which do not merge and do consume two.
    """
    rule = weekly(2)
    day = NOW - timedelta(days=1)
    unrelated_request = request_at(NOW)  # far enough from `day` that it never merges with it

    two_separate_sessions = context(at(day), at(day + timedelta(hours=3)))
    one_merged_session = context(
        BookingRecord(
            user_id=USER, resource_id=RESOURCE, start_at=day, end_at=day + timedelta(hours=2)
        )
    )

    # Two rows plus the request's own separate session already break a cap of two...
    assert not rule.evaluate(unrelated_request, two_separate_sessions).passed
    # ...but one record spanning the same stretch of the week is still only one entry.
    assert rule.evaluate(unrelated_request, one_merged_session).passed


def test_a_five_minute_gap_joins_when_the_tolerance_covers_it():
    """Mirrors ``app.rules_stub._resolve_run``'s own tolerance tests — the rule reads the identical
    kind of value, just supplied at construction instead of resolved from history at evaluate
    time."""
    rule = weekly(1, tolerance=timedelta(minutes=60))
    held = BookingRecord(
        user_id=USER,
        resource_id=RESOURCE,
        start_at=NOW - timedelta(hours=1, minutes=5),
        end_at=NOW - timedelta(minutes=5),
    )

    assert rule.evaluate(request_at(NOW), context(held)).passed


def test_history_is_counted_regardless_of_what_it_describes():
    """Everything in HistoryContext counts. There is no status to inspect and none is inferred.

    The engine is deliberately ignorant of a schema that will keep changing; an entry that should
    not count toward a limit is one the caller does not put in the context.
    """
    rule = weekly(1)
    assert not rule.evaluate(request_at(NOW), context(at(NOW - timedelta(days=1)))).passed


# --- the window itself ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_type", [MaxBookingsPerDayRule, MaxBookingsPerWeekRule, MaxBookingsPerMonthRule]
)
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


@pytest.mark.parametrize(
    "rule_type", [MaxBookingsPerDayRule, MaxBookingsPerWeekRule, MaxBookingsPerMonthRule]
)
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


# --- max_duration_per_day's own window (task 8.7) ----------------------------------------------
#
# Identical validation, a different constructor signature: `max_duration` is a `timedelta`, not a
# bookings count, so it cannot share the parametrized tests above without a positional `1` being
# read as an invalid `max_duration` and masking the window failure under test.


@pytest.mark.parametrize(
    "start, end",
    [
        (datetime(2026, 7, 13), datetime(2026, 7, 20)),
        (
            datetime(2026, 7, 13, tzinfo=timezone(timedelta(hours=10))),
            datetime(2026, 7, 20, tzinfo=timezone(timedelta(hours=10))),
        ),
    ],
)
def test_duration_per_day_window_that_is_not_utc_is_refused(start, end):
    with pytest.raises(ValueError):
        MaxDurationPerDayRule(timedelta(hours=1), window_start=start, window_end=end)


def test_duration_per_day_inverted_or_empty_window_is_refused():
    instant = datetime(2026, 7, 13, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        MaxDurationPerDayRule(
            timedelta(hours=1), window_start=instant, window_end=instant - timedelta(days=1)
        )
    with pytest.raises(ValueError):
        MaxDurationPerDayRule(timedelta(hours=1), window_start=instant, window_end=instant)
