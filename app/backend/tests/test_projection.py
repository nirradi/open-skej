"""Tests for ``app.projection`` and ``app.rules_stub.projectable_config`` — the design this branch
implements: project only the rules whose verdict is a pure function of the candidate interval and
the date (task 1), take existing bookings out of the projected canon and clip them in afterward by
interval arithmetic (task 2), and report each day's own slot size (task 3).

Pure Python, no database — every canon here is built by hand from ``SpaceRuleConfig`` /
``SpaceRuleRow``, the identical convention ``test_rules_stub.py`` already uses for the adapter this
module sits on top of. ``project_days`` runs the real ``rules.evaluate_request``; nothing here
reimplements a rule's semantics.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from rules import REGISTRY, RuleType
from rules import BaseRule, BookingRequest as EngineBookingRequest
from rules import CalendarContext, Context as EngineContext, HistoryContext, RuleResult, UserContext
from rules import Weekday

from app.projection import DayGrid, DayProjection, SlotProjection, clip_overlap, project_days
from app.routers.bookable import _grid_for_date
from app.rules_stub import (
    NotProjectedRuleType,
    SpaceRuleConfig,
    SpaceRuleRow,
    _build_canon,
    _build_local_frame,
    _local_date,
    _resolve_run,
    projectable_config,
)

# A fixed clock, well before every candidate date used below, so `NotInThePastRule` never
# interferes with what these tests actually check.
NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)

# 2026-08-17 is a real Monday (mirrors `app/backend/scripts/projection_bench.py`'s own
# `WINDOW_START`); 2026-08-18 is the Tuesday right after it.
MONDAY = date(2026, 8, 17)
TUESDAY = date(2026, 8, 18)


def _context_builder(tz_name: str = "UTC"):
    """The ``make_request_and_context`` callable every case below hands to ``project_days`` —
    mirrors ``app.routers.bookable._projection_context_builder`` exactly (same adapter functions,
    same empty-history run resolution), just built inline since these tests have no live
    ``SpaceRuleConfig`` closed over a real endpoint request.
    """
    zone = ZoneInfo(tz_name)

    def make_request_and_context(start_local: datetime, end_local: datetime):
        start_at = start_local.replace(tzinfo=zone).astimezone(timezone.utc)
        end_at = end_local.replace(tzinfo=zone).astimezone(timezone.utc)
        request = EngineBookingRequest(
            user_id="u", resource_id="r", start_at=start_at, end_at=end_at
        )
        on_date = _local_date(start_at, tz_name)
        local = _build_local_frame(request, tz_name, on_date)
        run = _resolve_run(request, (), timedelta(0))
        context = EngineContext(
            user=UserContext(user_id="u"),
            calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=NOW),
            local=local,
            run=run,
            history=HistoryContext(bookings=()),
        )
        return request, context

    return make_request_and_context


def _canon_for_day(config: SpaceRuleConfig):
    def build(on_date: date) -> tuple[BaseRule, ...]:
        return _build_canon(config, on_date)

    return build


# --- Task 1: only the cheap, exact rules are projected. --------------------------------------


def test_a_history_reading_rule_is_excluded_from_projection_and_reported():
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="availability_hours",
                params={"opens_at_minutes": 540, "closes_at_minutes": 1020},
            ),
            SpaceRuleRow(id=2, rule_type="max_bookings_per_week", params={"max_bookings": 3}),
        ),
    )

    projected, not_projected = projectable_config(config)

    assert [row.rule_type for row in projected.rules] == ["availability_hours"]
    assert not_projected == (
        NotProjectedRuleType(
            rule_type="max_bookings_per_week", label=REGISTRY["max_bookings_per_week"].label
        ),
    )


def test_a_disabled_history_reading_row_is_kept_and_not_reported():
    """A paused rule is not in force — reporting it in ``notProjected`` would tell a member about a
    constraint that does not actually apply. It stays in the config `_build_canon` sees (which
    drops it itself for being disabled), rather than being filtered here too."""
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1, rule_type="max_bookings_per_week", params={"max_bookings": 3}, enabled=False
            ),
        ),
    )

    projected, not_projected = projectable_config(config)

    assert not_projected == ()
    assert [row.rule_type for row in projected.rules] == ["max_bookings_per_week"]


def test_an_unregistered_rule_type_is_kept_not_reported_as_excluded():
    """A row naming no registered type is a configuration-integrity failure `_build_canon` fails
    closed on — a different thing entirely from a rule task 1 judges too expensive to project."""
    config = SpaceRuleConfig(
        timezone="UTC", rules=(SpaceRuleRow(id=1, rule_type="not_a_real_type", params={}),)
    )

    projected, not_projected = projectable_config(config)

    assert not_projected == ()
    assert [row.rule_type for row in projected.rules] == ["not_a_real_type"]


# --- Task 1 (continued): a generated-style rule that closes hours IS projected. ---------------


class _ClosedTuesdayAfternoonRule(BaseRule):
    """Stands in for a generated rule that reads only ``context.local`` — exactly the shape
    ``rules/generation`` produces and ``.claude/rules/rule-engine.md`` documents a generated rule
    as allowed to read."""

    def evaluate(self, request: EngineBookingRequest, context: EngineContext) -> RuleResult:
        local = context.local
        if local.weekday == 1 and local.start_minutes >= 780:  # Tuesday, 13:00 or later
            return RuleResult.deny("Closed Tuesday afternoons for scheduled maintenance.")
        return RuleResult.allow()


def _generated_style_lookup(rule_type: str):
    if rule_type == "closed_tuesday_pm":
        return RuleType(
            rule_type="closed_tuesday_pm",
            label="Closed Tuesday afternoons",
            description="Generated: refuses any booking on Tuesday from 13:00.",
            priority=100,
            params=(),
            reads_history=False,
            needs_local_resolution=False,
            is_single=False,
            build=lambda params, resolved: _ClosedTuesdayAfternoonRule(),
        )
    return REGISTRY.get(rule_type)


def test_a_generated_style_rule_that_closes_hours_is_projected_and_shades_the_grid():
    """This is the bug the whole design exists to fix
    (``ops/pending/bugs/calendar-does-not-reflect-the-rule-set.md``): a rule type the calendar does
    not special-case by name must still visibly shade the grid it draws."""
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(SpaceRuleRow(id=1, rule_type="closed_tuesday_pm", params={}),),
        lookup=_generated_style_lookup,
    )
    projected, not_projected = projectable_config(config)
    assert not_projected == ()  # reads_history=False — this type is projected, not excluded

    grid = DayGrid(date=TUESDAY, slot_minutes=60, first_slot_minutes=0, slot_count=24)
    (day,) = project_days(
        canon_for_day=_canon_for_day(projected),
        make_request_and_context=_context_builder(),
        days=(grid,),
        early_stop=True,
    )

    morning_slot = day.slots[9]  # 09:00
    afternoon_slot = day.slots[13]  # 13:00 — the generated rule's own cutoff

    assert morning_slot.min_slots > 0
    assert afternoon_slot.min_slots == 0
    assert afternoon_slot.max_slots == 0
    assert afternoon_slot.reason_text == "Closed Tuesday afternoons for scheduled maintenance."


# --- A duration cap limits max_slots rather than shading. -------------------------------------


def test_a_duration_cap_limits_max_slots_rather_than_shading():
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="availability_hours",
                params={"opens_at_minutes": 540, "closes_at_minutes": 1020},
            ),
            SpaceRuleRow(id=2, rule_type="max_duration", params={"max_duration_minutes": 120}),
        ),
    )
    projected, _ = projectable_config(config)

    grid = DayGrid(date=MONDAY, slot_minutes=60, first_slot_minutes=0, slot_count=24)
    (day,) = project_days(
        canon_for_day=_canon_for_day(projected),
        make_request_and_context=_context_builder(),
        days=(grid,),
        early_stop=True,
    )

    nine_am = day.slots[9]
    # Capped at 2 hours, not denied outright: the slot is still offered, just shorter.
    assert nine_am.min_slots == 1
    assert nine_am.max_slots == 2
    assert nine_am.reason_code is None
    assert nine_am.reason_text is None


# --- Task 2: overlap is applied after the scan, by interval arithmetic. -----------------------


def _built_day(config: SpaceRuleConfig, grid: DayGrid) -> DayProjection:
    (day,) = project_days(
        canon_for_day=_canon_for_day(config),
        make_request_and_context=_context_builder(),
        days=(grid,),
        early_stop=True,
    )
    return day


def test_clip_overlap_shortens_a_run_and_zeroes_a_covered_start():
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="availability_hours",
                params={"opens_at_minutes": 540, "closes_at_minutes": 720},  # 09:00-12:00
            ),
        ),
    )
    grid = DayGrid(date=MONDAY, slot_minutes=30, first_slot_minutes=540, slot_count=6)
    day = _built_day(config, grid)
    assert day.slots[0].max_slots == 6  # unclipped: 09:00 can run the full 3 hours

    # A booking from 10:00 (600) to 10:30 (630).
    clipped = clip_overlap(
        day, ((600.0, 630.0),), reason_code="already_booked", reason_text="Booked."
    )

    nine_am = clipped.slots[0]  # starts at 09:00, reaches 10:00 before the booking
    assert nine_am.min_slots == 1
    assert nine_am.max_slots == 2

    ten_am = clipped.slots[2]  # starts at 10:00 — inside the booking
    assert ten_am.min_slots == 0
    assert ten_am.max_slots == 0
    assert ten_am.reason_code == "already_booked"
    assert ten_am.reason_text == "Booked."

    ten_thirty = clipped.slots[3]  # starts at 10:30 — the booking's own end, not overlapping
    assert ten_thirty.min_slots == 1
    assert ten_thirty.max_slots == 3  # 10:30 to 12:00


def test_clip_overlap_never_overwrites_a_real_rule_denial():
    slot = SlotProjection(
        start_minutes=0, min_slots=0, max_slots=0, reason_code="deny_x", reason_text="Closed."
    )
    day = DayProjection(date=MONDAY, slot_minutes=30, first_slot_minutes=0, slots=(slot,))

    clipped = clip_overlap(day, ((0.0, 30.0),), reason_code="already_booked", reason_text="Booked.")

    assert clipped.slots[0].reason_code == "deny_x"
    assert clipped.slots[0].reason_text == "Closed."


def test_clip_overlap_zeroes_a_start_whose_minimum_length_now_collides():
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="availability_hours",
                params={"opens_at_minutes": 540, "closes_at_minutes": 720},
            ),
            SpaceRuleRow(id=2, rule_type="session_length", params={"session_minutes": 60}),
        ),
    )
    grid = DayGrid(date=MONDAY, slot_minutes=30, first_slot_minutes=540, slot_count=6)
    day = _built_day(config, grid)
    nine_am = day.slots[0]
    assert nine_am.min_slots == 2  # session_length: shortest allowed run is 60 minutes = 2 slots

    # A booking from 09:15 (555) to 09:45 (585) — inside the shortest length 09:00 could offer.
    clipped = clip_overlap(
        day, ((555.0, 585.0),), reason_code="already_booked", reason_text="Booked."
    )

    assert clipped.slots[0].min_slots == 0
    assert clipped.slots[0].max_slots == 0
    assert clipped.slots[0].reason_code == "already_booked"


# --- Task 3: each day's own slot size. ---------------------------------------------------------


def test_weekday_scoped_session_length_resolves_a_different_grid_per_day():
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="session_length",
                params={"session_minutes": 60},
                applies_to={"weekdays": [0, 1, 2, 3, 4]},
            ),
            SpaceRuleRow(
                id=2,
                rule_type="session_length",
                params={"session_minutes": 15},
                applies_to={"weekdays": [5, 6]},
            ),
        ),
    )

    saturday = date(2026, 8, 22)

    assert _grid_for_date(config, MONDAY).slot_minutes == 60
    assert _grid_for_date(config, saturday).slot_minutes == 15


def test_no_session_length_row_falls_back_to_the_drawing_resolution_constant():
    from app.routers.bookable import UNCONFIGURED_DAY_DRAWING_RESOLUTION_MINUTES

    config = SpaceRuleConfig(timezone="UTC", rules=())

    grid = _grid_for_date(config, MONDAY)

    assert grid.slot_minutes == UNCONFIGURED_DAY_DRAWING_RESOLUTION_MINUTES
