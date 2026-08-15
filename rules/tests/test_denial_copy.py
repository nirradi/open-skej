"""Guard: no rule the engine can run may put an absolute time in its denial copy.

WHY THIS EXISTS
----------------
Nothing in this contract carries a timezone, by design (`CalendarContext`, `LocalFrame`,
`.claude/rules/rule-engine.md`) — every datetime a rule holds is UTC, and UTC is not a fact about
any one venue's clock. If a rule's ``fail_reason`` prints a clock time, a calendar date, or a bare
zone name, it is printing UTC dressed up as if it meant something to the reader — and the reader,
a member looking at a refusal on their own screen, has no way to tell the difference from the time
they actually meant. Rendering the venue's own hours in the venue's own clock is the calendar UI's
job, done with a timezone the engine deliberately does not have.

If you are reading this because your new rule tripped it: state the limit in relative terms — a
duration, a count, a number of days — and, if the person needs the specifics, point them at the
calendar rather than trying to print the bound yourself.

This guard runs over two different sets of rules and neither covers the other: the hand-written
canon (`DEFAULT_CANON`) and every type the registry (`rules.registry.REGISTRY`) knows how to build.
A third guard, over the AI generation loop's system prompt, lives in
`rules/tests/test_generator.py` — generated rules are not part of either set here.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from rules.canon import DEFAULT_CANON, AvailabilityHoursRule
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
from rules.registry import REGISTRY

from tests.frames import utc_frame

USER = "u1"
RESOURCE = "court-1"

NOW = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


# --- the absolute-time check itself ----------------------------------------------------------
#
# Deliberately narrow — four concrete shapes an absolute time takes, not a broad "looks numeric"
# heuristic that would also flag "2 hours" or "60 minutes". See the pinning tests at the bottom of
# this module for the distinction this check has to hold.

_CLOCK_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")  # 17:00, 6:00
_MERIDIEM_HOUR = re.compile(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", re.IGNORECASE)  # 9am, 9 AM, 9:30 pm
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")  # 2026-07-20
_TIMEZONE_NAME = re.compile(r"\b(UTC|GMT)\b")  # a bare zone name

_ABSOLUTE_TIME_PATTERNS = (_CLOCK_TIME, _MERIDIEM_HOUR, _ISO_DATE, _TIMEZONE_NAME)


def _names_an_absolute_time(copy: str) -> bool:
    return any(pattern.search(copy) for pattern in _ABSOLUTE_TIME_PATTERNS)


# --- fixtures shared by every scenario below --------------------------------------------------


def request(start_at: datetime, end_at: datetime) -> BookingRequest:
    return BookingRequest(user_id=USER, resource_id=RESOURCE, start_at=start_at, end_at=end_at)


def context(
    now: datetime = NOW,
    *,
    frame_for: datetime | None = None,
    frame_end: datetime | None = None,
    history=(),
    run: RunContext | None = None,
) -> Context:
    # No rule reached through this module's `.evaluate(req, ctx)` calls reads `context.run` — see
    # `_assert_denies_with_no_absolute_time`, which never goes through `evaluate_request`'s
    # cross-check — so a fixed, inert span is enough here, with one exception:
    # `max_consecutive_duration` reads it directly, so its own scenario passes `run` explicitly.
    start = frame_for if frame_for is not None else now
    return Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=now),
        local=utc_frame(start, frame_end),
        run=run or RunContext(start_at=start, end_at=start + timedelta(hours=1), booking_count=1),
        history=HistoryContext(bookings=tuple(history)),
    )


def _assert_denies_with_no_absolute_time(rule, req: BookingRequest, ctx: Context) -> None:
    result = rule.evaluate(req, ctx)
    assert not result.passed, f"{type(rule).__name__} was expected to deny this scenario"
    assert result.fail_reason, f"{type(rule).__name__} denied with no fail_reason at all"
    assert not _names_an_absolute_time(
        result.fail_reason
    ), f"{type(rule).__name__} named an absolute time: {result.fail_reason!r}"


# --- DEFAULT_CANON ------------------------------------------------------------------------------

#: One denial scenario per rule in ``DEFAULT_CANON``, keyed by class name. The membership check in
#: the test below is what stops a rule added to the canon later from silently going unchecked here.
_DEFAULT_CANON_SCENARIOS = {
    "NotInThePastRule": lambda: (
        request(NOW - timedelta(minutes=5), NOW + timedelta(minutes=25)),
        context(),
    ),
    "BookingHorizonRule": lambda: (
        request(NOW + timedelta(days=61), NOW + timedelta(days=61, hours=1)),
        context(frame_for=NOW + timedelta(days=61)),
    ),
    "MaxDurationRule": lambda: (
        request(NOW, NOW + timedelta(hours=3)),
        context(),
    ),
    "AvailabilityHoursRule": lambda: (
        request(
            datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 20, 3, 30, tzinfo=timezone.utc),
        ),
        context(
            frame_for=datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc),
            frame_end=datetime(2026, 7, 20, 3, 30, tzinfo=timezone.utc),
        ),
    ),
}


def test_every_rule_in_the_default_canon_denies_without_naming_an_absolute_time():
    names = [type(rule).__name__ for rule in DEFAULT_CANON]
    assert set(names) == set(
        _DEFAULT_CANON_SCENARIOS
    ), "DEFAULT_CANON's own membership changed; add or remove a scenario above to match it"
    for rule in DEFAULT_CANON:
        req, ctx = _DEFAULT_CANON_SCENARIOS[type(rule).__name__]()
        _assert_denies_with_no_absolute_time(rule, req, ctx)


# --- Every type the registry can build ----------------------------------------------------------

_FRAME = utc_frame(NOW)

#: One scenario per registered rule type: the params (and, for a type with
#: ``needs_local_resolution``, the resolved values) the registry's own ``build`` function takes,
#: plus a request/context pair that drives the built rule into a denial. A type left out here is a
#: hole in this guard — the membership check in the test below is what catches that.
_REGISTRY_SCENARIOS: dict[str, dict] = {
    "not_in_the_past": {
        "params": {},
        "resolved": None,
        "case": lambda: (
            request(NOW - timedelta(minutes=5), NOW + timedelta(minutes=25)),
            context(),
        ),
    },
    "booking_horizon": {
        "params": {"days": 7},
        "resolved": None,
        "case": lambda: (
            request(NOW + timedelta(days=8), NOW + timedelta(days=8, hours=1)),
            context(frame_for=NOW + timedelta(days=8)),
        ),
    },
    "max_duration": {
        "params": {"max_duration_minutes": 60},
        "resolved": None,
        "case": lambda: (request(NOW, NOW + timedelta(hours=2)), context()),
    },
    "max_consecutive_duration": {
        "params": {"max_consecutive_minutes": 60},
        "resolved": None,
        "case": lambda: (
            request(NOW, NOW + timedelta(minutes=30)),
            context(
                run=RunContext(
                    start_at=NOW - timedelta(minutes=40),
                    end_at=NOW + timedelta(minutes=30),
                    booking_count=2,
                )
            ),
        ),
    },
    "session_length": {
        "params": {"session_minutes": 30},
        "resolved": {"anchor_minutes": 0},
        "case": lambda: (
            request(
                datetime(2026, 7, 20, 10, 7, tzinfo=timezone.utc),
                datetime(2026, 7, 20, 10, 37, tzinfo=timezone.utc),
            ),
            context(frame_for=datetime(2026, 7, 20, 10, 7, tzinfo=timezone.utc)),
        ),
    },
    "availability_hours": {
        "params": {"opens_at_minutes": 9 * 60, "closes_at_minutes": 17 * 60},
        "resolved": None,
        "case": lambda: (
            request(
                datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 20, 5, 30, tzinfo=timezone.utc),
            ),
            context(
                frame_for=datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc),
                frame_end=datetime(2026, 7, 20, 5, 30, tzinfo=timezone.utc),
            ),
        ),
    },
    "max_duration_per_day": {
        "params": {"max_duration_minutes": 60},
        # No `tolerance` — `MaxDurationPerDayRule` sums raw entries rather than merging them into
        # runs (`rules.frequency`'s module docstring), so `resolved` carries only the window.
        "resolved": {"window_start": _FRAME.day_start, "window_end": _FRAME.day_end},
        "case": lambda: (
            request(NOW, NOW + timedelta(minutes=45)),
            context(
                history=[
                    BookingRecord(
                        user_id=USER,
                        resource_id=RESOURCE,
                        start_at=NOW - timedelta(hours=2),
                        end_at=NOW - timedelta(hours=1, minutes=30),
                    )
                ]
            ),
        ),
    },
    "max_bookings_per_day": {
        "params": {"max_bookings": 1},
        "resolved": {
            "window_start": _FRAME.day_start,
            "window_end": _FRAME.day_end,
            "tolerance": timedelta(0),
        },
        "case": lambda: (
            request(NOW, NOW + timedelta(hours=1)),
            context(
                history=[
                    BookingRecord(
                        user_id=USER,
                        resource_id=RESOURCE,
                        start_at=NOW - timedelta(hours=2),
                        end_at=NOW - timedelta(hours=1),
                    )
                ]
            ),
        ),
    },
    "max_bookings_per_week": {
        "params": {"max_bookings": 1},
        # `tolerance` (task 8.6): `evaluate` merges `request` with `context.history.bookings`
        # itself now, at zero tolerance here (exact abutment only) — see `rules.frequency`'s module
        # docstring. The single history entry below does not abut the request, so the merge leaves
        # it and the request as two separate sessions, over the cap of one.
        "resolved": {
            "window_start": _FRAME.week_start,
            "window_end": _FRAME.week_end,
            "tolerance": timedelta(0),
        },
        "case": lambda: (
            request(NOW, NOW + timedelta(hours=1)),
            context(
                history=[
                    BookingRecord(
                        user_id=USER,
                        resource_id=RESOURCE,
                        start_at=NOW - timedelta(hours=2),
                        end_at=NOW - timedelta(hours=1),
                    )
                ]
            ),
        ),
    },
    "max_bookings_per_month": {
        "params": {"max_bookings": 1},
        "resolved": {
            "window_start": _FRAME.month_start,
            "window_end": _FRAME.month_end,
            "tolerance": timedelta(0),
        },
        "case": lambda: (
            request(NOW, NOW + timedelta(hours=1)),
            context(
                history=[
                    BookingRecord(
                        user_id=USER,
                        resource_id=RESOURCE,
                        start_at=NOW - timedelta(days=1),
                        end_at=NOW - timedelta(days=1) + timedelta(hours=1),
                    )
                ]
            ),
        ),
    },
}


def test_every_registered_type_denies_without_naming_an_absolute_time():
    assert set(_REGISTRY_SCENARIOS) == set(
        REGISTRY
    ), "REGISTRY's own membership changed; add or remove a scenario above to match it"
    for rule_type_id, scenario in _REGISTRY_SCENARIOS.items():
        rule_type = REGISTRY[rule_type_id]
        rule = rule_type.build(scenario["params"], scenario["resolved"])
        req, ctx = scenario["case"]()
        _assert_denies_with_no_absolute_time(rule, req, ctx)


# --- Both of the availability rule's branches -----------------------------------------------------
#
# The two sweeps above drive one denial per rule, which for `AvailabilityHoursRule` is the
# "before we open" branch. It has a second one, and each branch carries its own copy — so each is
# its own chance to reintroduce a bound as a clock time. This is where the times actually were.


def test_the_availability_rule_names_no_time_on_either_side_of_its_window():
    rule = AvailabilityHoursRule(opens_at_minutes=9 * 60, closes_at_minutes=17 * 60)
    day = datetime(2026, 7, 20, tzinfo=timezone.utc)

    before_open = request(day.replace(hour=5), day.replace(hour=5, minute=30))
    after_close = request(day.replace(hour=16, minute=30), day.replace(hour=18))

    _assert_denies_with_no_absolute_time(
        rule,
        before_open,
        context(frame_for=before_open.start_at, frame_end=before_open.end_at),
    )
    _assert_denies_with_no_absolute_time(
        rule,
        after_close,
        context(frame_for=after_close.start_at, frame_end=after_close.end_at),
    )


# --- A duration is not an absolute time ---------------------------------------------------------
#
# "Bookings can be at most 2 hours long" is contract copy `app/e2e/tests/03-sad-path.spec.ts`
# asserts as a full-string match, and it must stay legal. A check that cannot tell "2 hours" from
# "17:00" would either fail this whole module immediately or get loosened until it catches nothing
# — these two tables are what keep it honest.

#: The exact message `test_the_max_duration_denial_copy_is_exact` in `test_canon.py` pins.
_MAX_DURATION_MESSAGE = (
    "Bookings can be at most 2 hours long, and this one is 2 hours and 30 minutes."
    " Please shorten it and try again."
)


@pytest.mark.parametrize(
    "copy",
    [
        "closes at 17:00",
        "opens at 9am",
        "before 9 AM",
        "on 2026-07-20",
        "23:00 UTC",
    ],
)
def test_the_check_flags_genuine_absolute_times(copy):
    assert _names_an_absolute_time(copy)


@pytest.mark.parametrize(
    "copy",
    [
        "at most 2 hours long",
        "60 minutes",
        "up to 60 days ahead",
        "at most 2 bookings a week",
        "this Space's 30-minute grid",
        _MAX_DURATION_MESSAGE,
    ],
)
def test_the_check_does_not_flag_relative_copy(copy):
    assert not _names_an_absolute_time(copy)
