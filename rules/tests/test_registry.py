"""Tests for the rule type registry.

Four things are pinned here, for different reasons.

**The schema itself** — the ten stable ids, each with the exact param names its underlying rule
class's constructor needs (read against ``canon.py`` / ``frequency.py`` directly, not against this
module's own memory of them), the documented priority order, and which flags are set on which type.
A registry that silently drifted from any of these would still import cleanly and would still look
like a registry; only a test that checks the values catches it.

**That priority reads as an order, not just as numbers.** ``rule_types()`` sorting is the whole
mechanism that replaces the hardcoded tuple ``rule-engine.md`` used to describe the canon as — a bug
here is invisible until a later task reads it to assemble a canon and gets the wrong sequence.

**That ``build`` is wired correctly, not just that it does not raise.** A build function that
constructs the wrong class, or drops a parameter on the floor, still "succeeds" if all that is
checked is the absence of an exception. Comparing ``build(...).evaluate(...)`` against the same rule
class constructed directly, on an input engineered to actually distinguish pass from fail, is what
catches a build function silently ignoring the parameters it was handed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rules.canon import (
    AvailabilityHoursRule,
    BookingHorizonRule,
    MaxConsecutiveDurationRule,
    MaxDurationRule,
    NotInThePastRule,
    SessionLengthRule,
)
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
from rules.registry import REGISTRY, ParamKind, rule_types
from tests.frames import utc_frame

USER = "u1"
RESOURCE = "court-1"

#: Mid-morning on an ordinary day, matching the convention `test_canon.py` uses for the same reason:
#: comfortably inside default hours, so a denial can only have come from the rule under test.
NOW = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


def request(start_at: datetime, end_at: datetime) -> BookingRequest:
    return BookingRequest(user_id=USER, resource_id=RESOURCE, start_at=start_at, end_at=end_at)


def context(
    *bookings: BookingRecord,
    now: datetime = NOW,
    frame_for: datetime | None = None,
    frame_end: datetime | None = None,
    run: RunContext | None = None,
) -> Context:
    """Most rules here read only the request and their own parameters, so the frame's exact bounds
    are inert for them and the default (anchored on ``now``) is fine. ``AvailabilityHoursRule``
    reads ``context.local`` directly, so its own tests pass ``frame_for``/``frame_end`` matching the
    request under test — see ``test_build_availability_hours_reads_raw_minutes_params_directly``.

    Every comparison here is ``build(...).evaluate(...)`` against the rule constructed directly
    (module docstring), never ``evaluate_request``, so ``context.run`` is inert the same way the
    frame is for most types here, with one exception: ``max_consecutive_duration`` reads it
    directly, so its own build-wiring test passes ``run`` explicitly rather than taking the inert
    one-hour default every other type here is happy to ignore.
    """
    start = frame_for if frame_for is not None else now
    return Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=now),
        local=utc_frame(start, frame_end),
        run=run or RunContext(start_at=start, end_at=start + timedelta(hours=1), booking_count=1),
        history=HistoryContext(bookings=bookings),
    )


def existing_booking(start_at: datetime) -> BookingRecord:
    return BookingRecord(
        user_id=USER, resource_id=RESOURCE, start_at=start_at, end_at=start_at + timedelta(hours=1)
    )


# --- the eleven stable ids, and their declared params -------------------------------------------

EXPECTED_IDS = {
    "not_in_the_past",
    "booking_horizon",
    "max_duration",
    "max_consecutive_duration",
    "session_length",
    "availability_hours",
    "max_duration_per_day",
    "max_bookings_per_day",
    "max_bookings_per_week",
    "max_bookings_per_month",
}


def test_all_ten_stable_ids_are_registered():
    assert set(REGISTRY) == EXPECTED_IDS


@pytest.mark.parametrize(
    "rule_type, expected_param_names",
    [
        ("not_in_the_past", ()),
        ("booking_horizon", ("days",)),
        ("max_duration", ("max_duration_minutes",)),
        ("max_consecutive_duration", ("max_consecutive_minutes",)),
        ("session_length", ("session_minutes",)),
        ("availability_hours", ("opens_at_minutes", "closes_at_minutes")),
        ("max_duration_per_day", ("max_duration_minutes",)),
        ("max_bookings_per_day", ("max_bookings",)),
        ("max_bookings_per_week", ("max_bookings",)),
        ("max_bookings_per_month", ("max_bookings",)),
    ],
)
def test_each_type_declares_exactly_the_params_its_constructor_needs(
    rule_type, expected_param_names
):
    params = REGISTRY[rule_type].params
    assert tuple(param.name for param in params) == expected_param_names


def test_stable_ids_are_never_the_python_class_name():
    """The whole point of a stable id: `AvailabilityHoursRule` the class could be renamed without
    this id moving, because nothing here is derived from `type(...).__name__`."""
    for rule_type, declared in REGISTRY.items():
        assert rule_type == rule_type.lower()
        assert declared.rule_type == rule_type


# --- priority order --------------------------------------------------------------------------


def test_priorities_sort_into_the_documented_canon_order():
    """`rule-engine.md`'s ten-element assembled order, now read off declared priority."""
    assert [declared.rule_type for declared in rule_types()] == [
        "not_in_the_past",
        "booking_horizon",
        "max_duration",
        "max_consecutive_duration",
        "session_length",
        "availability_hours",
        "max_duration_per_day",
        "max_bookings_per_day",
        "max_bookings_per_week",
        "max_bookings_per_month",
    ]


def test_session_length_sits_strictly_between_max_consecutive_duration_and_availability_hours():
    """`session_length`'s priority is beside the two duration rules, not with the date or counting
    rules — the plan's own reasoning for where task 6.5 inserted `slot_alignment` (the type this
    one replaces, at the identical priority), unmoved by task 8.5 landing a third rule in that band
    beside it."""
    assert (
        REGISTRY["max_consecutive_duration"].priority
        < REGISTRY["session_length"].priority
        < REGISTRY["availability_hours"].priority
    )


def test_max_consecutive_duration_sits_strictly_between_max_duration_and_session_length():
    """`max_consecutive_duration`'s priority (32) is between `max_duration` (30) and
    `session_length` (35) — task 8.5's own reasoning: a booking that breaks both duration rules at
    once is more usefully told to shorten itself than to stop abutting a neighbour, so
    `max_duration` keeps first refusal, and 32 leaves room on both sides for a later insertion."""
    assert (
        REGISTRY["max_duration"].priority
        < REGISTRY["max_consecutive_duration"].priority
        < REGISTRY["session_length"].priority
    )


def test_max_duration_per_day_sits_strictly_between_availability_hours_and_max_bookings_per_day():
    """`max_duration_per_day`'s priority (42) is between `availability_hours` (40) and
    `max_bookings_per_day` (45) — task 8.7's own reasoning: of the day/week/month caps a user could
    break at once, the narrowest window is the most useful thing to be told, so the day-scoped pair
    sits ahead of the week and month rules, and the duration total precedes the booking count within
    that pair."""
    assert (
        REGISTRY["availability_hours"].priority
        < REGISTRY["max_duration_per_day"].priority
        < REGISTRY["max_bookings_per_day"].priority
    )


def test_max_bookings_per_day_sits_strictly_between_max_duration_per_day_and_the_weekly_cap():
    """`max_bookings_per_day`'s priority (45) is between `max_duration_per_day` (42) and
    `max_bookings_per_week` (50) — the narrowest counting window goes first among the three."""
    assert (
        REGISTRY["max_duration_per_day"].priority
        < REGISTRY["max_bookings_per_day"].priority
        < REGISTRY["max_bookings_per_week"].priority
    )


def test_priorities_are_unique_and_spaced_for_a_later_insertion():
    """Priorities are spaced in multiples of ten rather than consecutive integers, so a later type
    can still be inserted between two existing ones without renumbering the rest — exactly what let
    `slot_alignment` (now `session_length`, at the identical priority) land at 35 without moving
    `availability_hours` off 40."""
    priorities = [declared.priority for declared in rule_types()]
    assert priorities == sorted(priorities)
    assert len(set(priorities)) == len(priorities)

    gap = REGISTRY["availability_hours"].priority - REGISTRY["session_length"].priority
    assert gap > 1


# --- reads_history / needs_local_resolution / is_single ---------------------------------------


def test_reads_history_is_true_for_the_counting_and_duration_total_types():
    """`reads_history` means "this rule's verdict depends on history", not "this rule's own
    `evaluate` names `context.history`" (`rules/rules/registry.py`, `.claude/rules/rule-engine.md`).
    `max_consecutive_duration` is the type that pulls the two apart: its `evaluate` reads only
    `context.run`, never `context.history` directly, but the run itself is resolved from history by
    the adapter, so a Space configuring this rule and nothing else must still make the router run
    the Space-wide history query — see `app/backend/tests/test_rules_stub.py` for that property
    pinned end to end."""
    assert {rt.rule_type for rt in REGISTRY.values() if rt.reads_history} == {
        "max_duration_per_day",
        "max_bookings_per_day",
        "max_bookings_per_week",
        "max_bookings_per_month",
        "max_consecutive_duration",
    }


def test_needs_local_resolution_is_true_for_session_length_and_the_history_reading_types():
    """`session_length` needs its `anchor_minutes` resolved against the Space's zone and the
    booking's own date, the same reason the day/week/month rules need local resolution.
    `availability_hours` does not: it stores minutes-from-local-midnight directly and reads
    `context.local` itself, so its build function needs nothing resolved for it
    (`rules/rules/registry.py`)."""
    assert {rt.rule_type for rt in REGISTRY.values() if rt.needs_local_resolution} == {
        "session_length",
        "max_duration_per_day",
        "max_bookings_per_day",
        "max_bookings_per_week",
        "max_bookings_per_month",
    }


def test_is_single_is_true_only_for_the_four_non_day_scoped_types():
    """`availability_hours`, `max_duration`, `max_consecutive_duration` and `session_length` are
    meant to vary by day via `applies_to` (e.g. "Mon/Wed/Fri 10-15" plus "Tue/Thu 8-12" as two
    `availability_hours` rows), so a second instance of any of them is the intended pattern, not a
    mistake worth warning about — `max_consecutive_duration` for the identical reason
    `max_duration` already gives."""
    assert {rt.rule_type for rt in REGISTRY.values() if rt.is_single} == {
        "not_in_the_past",
        "booking_horizon",
        "max_duration_per_day",
        "max_bookings_per_day",
        "max_bookings_per_week",
        "max_bookings_per_month",
    }
    assert {rt.rule_type for rt in REGISTRY.values() if not rt.is_single} == {
        "max_duration",
        "max_consecutive_duration",
        "session_length",
        "availability_hours",
    }


# --- bounds -----------------------------------------------------------------------------------


def test_every_integer_param_has_a_positive_minimum():
    integer_params = [
        param
        for declared in REGISTRY.values()
        for param in declared.params
        if param.kind is ParamKind.INTEGER
    ]
    assert integer_params  # sanity: the parametrisation below isn't vacuous
    for param in integer_params:
        assert param.minimum == 1
        assert param.required is True


def test_local_time_params_are_bounded_within_a_single_day():
    """Both are stored as minutes from local midnight (``rules/rules/registry.py``): a value below
    zero, or a bare ``closes_at_minutes`` of zero, is meaningless on its own — the pairwise relation
    between the two is a separate check `AvailabilityHoursRule`'s own constructor makes."""
    assert REGISTRY["availability_hours"].params[0].name == "opens_at_minutes"
    assert REGISTRY["availability_hours"].params[0].minimum == 0
    assert REGISTRY["availability_hours"].params[1].name == "closes_at_minutes"
    assert REGISTRY["availability_hours"].params[1].minimum == 1


# --- build() behaves identically to constructing the class directly ---------------------------


def test_build_not_in_the_past_behaves_like_the_class():
    built = REGISTRY["not_in_the_past"].build({})
    direct = NotInThePastRule()
    stale = request(NOW - timedelta(minutes=1), NOW + timedelta(hours=1))
    assert built.evaluate(stale, context()) == direct.evaluate(stale, context())
    assert not built.evaluate(stale, context()).passed


def test_build_booking_horizon_behaves_like_the_class():
    built = REGISTRY["booking_horizon"].build({"days": 60})
    direct = BookingHorizonRule(days=60)
    beyond = request(NOW + timedelta(days=61), NOW + timedelta(days=61, hours=1))
    assert built.evaluate(beyond, context()) == direct.evaluate(beyond, context())
    assert not built.evaluate(beyond, context()).passed


def test_build_max_duration_behaves_like_the_class():
    built = REGISTRY["max_duration"].build({"max_duration_minutes": 120})
    direct = MaxDurationRule(max_duration=timedelta(hours=2))
    over = request(NOW, NOW + timedelta(hours=2, minutes=30))
    assert built.evaluate(over, context()) == direct.evaluate(over, context())
    assert not built.evaluate(over, context()).passed


def test_build_max_consecutive_duration_behaves_like_the_class():
    """Unlike every build test above, the request itself is short — the point is that this type
    judges `context.run`, not `request.duration`, so a passing test here has to prove the built
    rule reads the *run* it was handed rather than silently ignoring `max_consecutive_minutes` and
    always allowing (a build function dropping the parameter on the floor would still pass every
    other assertion in this module)."""
    built = REGISTRY["max_consecutive_duration"].build({"max_consecutive_minutes": 120})
    direct = MaxConsecutiveDurationRule(max_duration=timedelta(hours=2))

    one_hour_request = request(NOW, NOW + timedelta(hours=1))
    joined_run = RunContext(
        start_at=NOW, end_at=NOW + timedelta(hours=2, minutes=30), booking_count=2
    )
    over_ctx = context(run=joined_run)
    assert built.evaluate(one_hour_request, over_ctx) == direct.evaluate(one_hour_request, over_ctx)
    assert not built.evaluate(one_hour_request, over_ctx).passed


def test_build_session_length_reads_resolved_anchor_not_a_raw_param():
    """`SessionLengthRule` needs its `anchor_minutes` resolved — the constructor this build
    function calls takes it from `resolved`, never from `params`, since resolving the date's own
    opening minutes (or the local-midnight fallback) is the adapter's job. `availability_hours`
    needs no such treatment: it stores minutes-from-local-midnight directly and needs nothing
    resolved (see the test below).
    """
    resolved = {"anchor_minutes": 0}
    built = REGISTRY["session_length"].build({"session_minutes": 30}, resolved)
    direct = SessionLengthRule(session_minutes=30, anchor_minutes=0)

    off_grid = request(NOW + timedelta(minutes=7), NOW + timedelta(minutes=37))
    off_grid_ctx = context(frame_for=off_grid.start_at, frame_end=off_grid.end_at)
    assert built.evaluate(off_grid, off_grid_ctx) == direct.evaluate(off_grid, off_grid_ctx)
    assert not built.evaluate(off_grid, off_grid_ctx).passed

    on_grid = request(NOW, NOW + timedelta(minutes=30))
    on_grid_ctx = context(frame_for=NOW, frame_end=NOW + timedelta(minutes=30))
    assert built.evaluate(on_grid, on_grid_ctx).passed


def test_build_availability_hours_reads_raw_minutes_params_directly():
    """`AvailabilityHoursRule` needs no local resolution any more (`needs_local_resolution=False`)
    — the constructor this build function calls reads the two minutes-from-local-midnight params
    straight off the stored row, exactly like `max_duration` or `booking_horizon` above."""
    params = {"opens_at_minutes": 6 * 60, "closes_at_minutes": 23 * 60}
    built = REGISTRY["availability_hours"].build(params)
    direct = AvailabilityHoursRule(opens_at_minutes=6 * 60, closes_at_minutes=23 * 60)

    too_early = request(
        datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 5, 30, tzinfo=timezone.utc),
    )
    too_early_context = context(frame_for=too_early.start_at, frame_end=too_early.end_at)
    assert built.evaluate(too_early, too_early_context) == direct.evaluate(
        too_early, too_early_context
    )
    assert not built.evaluate(too_early, too_early_context).passed

    inside = request(NOW, NOW + timedelta(hours=1))
    assert built.evaluate(inside, context(frame_for=NOW, frame_end=NOW + timedelta(hours=1))).passed


def test_build_max_bookings_per_week_behaves_like_the_class():
    window_start = datetime(2026, 7, 13, tzinfo=timezone.utc)
    window_end = datetime(2026, 7, 20, tzinfo=timezone.utc)
    # `tolerance` (task 8.6): `evaluate` merges `request` with history itself now — see
    # `rules.frequency`'s module docstring. Zero here means exact abutment only, so the one
    # history entry below (not abutting the request) stays a separate session.
    resolved = {"window_start": window_start, "window_end": window_end, "tolerance": timedelta(0)}

    built = REGISTRY["max_bookings_per_week"].build({"max_bookings": 1}, resolved)
    direct = MaxBookingsPerWeekRule(
        max_bookings=1, window_start=window_start, window_end=window_end, tolerance=timedelta(0)
    )

    already_full = context(existing_booking(NOW - timedelta(days=1)))
    # Within `[window_start, window_end)` itself, unlike module-level `NOW` (2026-07-20 10:00),
    # which the hardcoded window above does not cover — the rule now merges the request into the
    # window count too (task 8.6), so a request the window does not contain would never be denied.
    req = request(window_end - timedelta(hours=1), window_end)
    assert built.evaluate(req, already_full) == direct.evaluate(req, already_full)
    assert not built.evaluate(req, already_full).passed


def test_build_max_bookings_per_month_behaves_like_the_class():
    window_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    window_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    resolved = {"window_start": window_start, "window_end": window_end, "tolerance": timedelta(0)}

    built = REGISTRY["max_bookings_per_month"].build({"max_bookings": 1}, resolved)
    direct = MaxBookingsPerMonthRule(
        max_bookings=1, window_start=window_start, window_end=window_end, tolerance=timedelta(0)
    )

    already_full = context(existing_booking(NOW - timedelta(days=1)))
    req = request(NOW, NOW + timedelta(hours=1))
    assert built.evaluate(req, already_full) == direct.evaluate(req, already_full)
    assert not built.evaluate(req, already_full).passed


def test_build_max_bookings_per_day_behaves_like_the_class():
    """Task 8.7. Built exactly like `max_bookings_per_week` above, against a narrower window."""
    window_start = datetime(2026, 7, 20, tzinfo=timezone.utc)
    window_end = datetime(2026, 7, 21, tzinfo=timezone.utc)
    resolved = {"window_start": window_start, "window_end": window_end, "tolerance": timedelta(0)}

    built = REGISTRY["max_bookings_per_day"].build({"max_bookings": 1}, resolved)
    direct = MaxBookingsPerDayRule(
        max_bookings=1, window_start=window_start, window_end=window_end, tolerance=timedelta(0)
    )

    already_full = context(existing_booking(NOW - timedelta(hours=2)))
    req = request(NOW, NOW + timedelta(hours=1))
    assert built.evaluate(req, already_full) == direct.evaluate(req, already_full)
    assert not built.evaluate(req, already_full).passed


def test_build_max_duration_per_day_behaves_like_the_class():
    """Task 8.7. Unlike every counting build function above, `resolved` carries no `tolerance` —
    `MaxDurationPerDayRule` sums raw entries and never merges (`rules.frequency`'s module
    docstring), so there is nothing for a gap tolerance to do."""
    window_start = datetime(2026, 7, 20, tzinfo=timezone.utc)
    window_end = datetime(2026, 7, 21, tzinfo=timezone.utc)
    resolved = {"window_start": window_start, "window_end": window_end}

    built = REGISTRY["max_duration_per_day"].build({"max_duration_minutes": 60}, resolved)
    direct = MaxDurationPerDayRule(
        max_duration=timedelta(minutes=60), window_start=window_start, window_end=window_end
    )

    already_booked = context(existing_booking(NOW - timedelta(hours=2)))
    req = request(NOW, NOW + timedelta(minutes=30))
    assert built.evaluate(req, already_booked) == direct.evaluate(req, already_booked)
    assert not built.evaluate(req, already_booked).passed
