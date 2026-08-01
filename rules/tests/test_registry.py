"""Tests for the rule type registry.

Four things are pinned here, for different reasons.

**The schema itself** — the seven stable ids, each with the exact param names its underlying rule
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

from datetime import datetime, time, timedelta, timezone

import pytest

from rules.canon import (
    AvailabilityHoursRule,
    BookingHorizonRule,
    MaxDurationRule,
    NotInThePastRule,
    SlotAlignmentRule,
)
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
from rules.registry import REGISTRY, ParamKind, rule_types

USER = "u1"
RESOURCE = "court-1"

#: Mid-morning on an ordinary day, matching the convention `test_canon.py` uses for the same reason:
#: comfortably inside default hours, so a denial can only have come from the rule under test.
NOW = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


def request(start_at: datetime, end_at: datetime) -> BookingRequest:
    return BookingRequest(user_id=USER, resource_id=RESOURCE, start_at=start_at, end_at=end_at)


def context(*bookings: BookingRecord, now: datetime = NOW) -> Context:
    return Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=now),
        history=HistoryContext(bookings=bookings),
    )


def existing_booking(start_at: datetime) -> BookingRecord:
    return BookingRecord(
        user_id=USER, resource_id=RESOURCE, start_at=start_at, end_at=start_at + timedelta(hours=1)
    )


# --- the seven stable ids, and their declared params ------------------------------------------

EXPECTED_IDS = {
    "not_in_the_past",
    "booking_horizon",
    "max_duration",
    "slot_alignment",
    "availability_hours",
    "max_bookings_per_week",
    "max_bookings_per_month",
}


def test_all_seven_stable_ids_are_registered():
    assert set(REGISTRY) == EXPECTED_IDS


@pytest.mark.parametrize(
    "rule_type, expected_param_names",
    [
        ("not_in_the_past", ()),
        ("booking_horizon", ("days",)),
        ("max_duration", ("max_duration_minutes",)),
        ("slot_alignment", ("slot_minutes",)),
        ("availability_hours", ("opens_at", "closes_at")),
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
    """`rule-engine.md`'s seven-element assembled order, now read off declared priority."""
    assert [declared.rule_type for declared in rule_types()] == [
        "not_in_the_past",
        "booking_horizon",
        "max_duration",
        "slot_alignment",
        "availability_hours",
        "max_bookings_per_week",
        "max_bookings_per_month",
    ]


def test_slot_alignment_sits_strictly_between_max_duration_and_availability_hours():
    """`slot_alignment`'s priority is beside `max_duration`, not with the date or counting rules —
    the plan's own reasoning for where task 6.5 inserted it."""
    assert (
        REGISTRY["max_duration"].priority
        < REGISTRY["slot_alignment"].priority
        < REGISTRY["availability_hours"].priority
    )


def test_priorities_are_unique_and_spaced_for_a_later_insertion():
    """Priorities are spaced in multiples of ten rather than consecutive integers, so a later type
    can still be inserted between two existing ones without renumbering the rest — exactly what
    let `slot_alignment` land at 35 without moving `availability_hours` off 40."""
    priorities = [declared.priority for declared in rule_types()]
    assert priorities == sorted(priorities)
    assert len(set(priorities)) == len(priorities)

    gap = REGISTRY["availability_hours"].priority - REGISTRY["slot_alignment"].priority
    assert gap > 1


# --- reads_history / needs_local_resolution / is_single ---------------------------------------


def test_reads_history_is_true_only_for_the_two_counting_types():
    assert {rt.rule_type for rt in REGISTRY.values() if rt.reads_history} == {
        "max_bookings_per_week",
        "max_bookings_per_month",
    }


def test_needs_local_resolution_is_true_for_hours_slot_alignment_and_the_two_counting_types():
    """`slot_alignment` needs its `anchor` resolved against the Space's zone and the booking's own
    date, the same reason `availability_hours` and the counting rules need local resolution."""
    assert {rt.rule_type for rt in REGISTRY.values() if rt.needs_local_resolution} == {
        "slot_alignment",
        "availability_hours",
        "max_bookings_per_week",
        "max_bookings_per_month",
    }


def test_is_single_is_true_only_for_the_four_non_day_scoped_types():
    """`availability_hours`, `max_duration` and `slot_alignment` are meant to vary by day via
    `applies_to` (e.g. "Mon/Wed/Fri 10-15" plus "Tue/Thu 8-12" as two `availability_hours` rows), so
    a second instance of any of them is the intended pattern, not a mistake worth warning about."""
    assert {rt.rule_type for rt in REGISTRY.values() if rt.is_single} == {
        "not_in_the_past",
        "booking_horizon",
        "max_bookings_per_week",
        "max_bookings_per_month",
    }
    assert {rt.rule_type for rt in REGISTRY.values() if not rt.is_single} == {
        "max_duration",
        "slot_alignment",
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


def test_local_time_params_have_no_numeric_bound():
    time_params = [
        param
        for declared in REGISTRY.values()
        for param in declared.params
        if param.kind is ParamKind.LOCAL_TIME
    ]
    assert time_params
    for param in time_params:
        assert param.minimum is None


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


def test_build_slot_alignment_reads_resolved_anchor_not_a_raw_param():
    """`SlotAlignmentRule` needs a UTC `anchor` instant — the constructor this build function calls
    takes it from `resolved`, never from `params`, since resolving the Space's local midnight for
    the booking's own date is a future adapter's job, exactly like `availability_hours`'s UTC pair.
    """
    anchor = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
    resolved = {"anchor": anchor}
    built = REGISTRY["slot_alignment"].build({"slot_minutes": 30}, resolved)
    direct = SlotAlignmentRule(slot_minutes=30, anchor=anchor)

    off_grid = request(NOW + timedelta(minutes=7), NOW + timedelta(minutes=37))
    assert built.evaluate(off_grid, context()) == direct.evaluate(off_grid, context())
    assert not built.evaluate(off_grid, context()).passed

    on_grid = request(NOW, NOW + timedelta(minutes=30))
    assert built.evaluate(on_grid, context()).passed


def test_build_availability_hours_reads_resolved_utc_times_not_raw_params():
    """`AvailabilityHoursRule` only ever speaks UTC — the constructor this build function calls
    takes the *resolved* UTC pair, never the Space's raw local `opens_at`/`closes_at`, since
    resolving local hours to a UTC window per date is a future adapter's job."""
    resolved = {"opens_at": time(6, 0), "closes_at": time(23, 0)}
    built = REGISTRY["availability_hours"].build({}, resolved)
    direct = AvailabilityHoursRule(opens_at=time(6, 0), closes_at=time(23, 0))

    too_early = request(
        datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 5, 30, tzinfo=timezone.utc),
    )
    assert built.evaluate(too_early, context()) == direct.evaluate(too_early, context())
    assert not built.evaluate(too_early, context()).passed

    inside = request(NOW, NOW + timedelta(hours=1))
    assert built.evaluate(inside, context()).passed


def test_build_max_bookings_per_week_behaves_like_the_class():
    window_start = datetime(2026, 7, 13, tzinfo=timezone.utc)
    window_end = datetime(2026, 7, 20, tzinfo=timezone.utc)
    resolved = {"window_start": window_start, "window_end": window_end}

    built = REGISTRY["max_bookings_per_week"].build({"max_bookings": 1}, resolved)
    direct = MaxBookingsPerWeekRule(
        max_bookings=1, window_start=window_start, window_end=window_end
    )

    already_full = context(existing_booking(NOW - timedelta(days=1)))
    req = request(NOW, NOW + timedelta(hours=1))
    assert built.evaluate(req, already_full) == direct.evaluate(req, already_full)
    assert not built.evaluate(req, already_full).passed


def test_build_max_bookings_per_month_behaves_like_the_class():
    window_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    window_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    resolved = {"window_start": window_start, "window_end": window_end}

    built = REGISTRY["max_bookings_per_month"].build({"max_bookings": 1}, resolved)
    direct = MaxBookingsPerMonthRule(
        max_bookings=1, window_start=window_start, window_end=window_end
    )

    already_full = context(existing_booking(NOW - timedelta(days=1)))
    req = request(NOW, NOW + timedelta(hours=1))
    assert built.evaluate(req, already_full) == direct.evaluate(req, already_full)
    assert not built.evaluate(req, already_full).passed
