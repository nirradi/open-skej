"""``GET /spaces/{public_id}/resources/{resource_id}/bookable`` — the projection, live.

A read-only window onto ``app.projection.project_days`` run against a **real** Space: its real
``space_rules`` canon (assembled per day, task 1's filtered ``canon_for_day``), and — after the
projection itself has already run — the Resource's real existing bookings, clipped in afterward by
interval arithmetic (task 2's ``app.projection.clip_overlap``). Nothing here reimplements a rule's
semantics or the adapter's own local-time resolution; every piece is the same machinery
``app.routers.resource_bookings`` and ``GET /spaces/{public_id}/schedule`` already call, reused
rather than re-derived (module docstrings on ``app/backend/app/rules_stub.py`` and
``app/backend/app/projection.py`` both make this the load-bearing rule for this whole area of the
codebase, and this endpoint is not the place to start breaking it).

**Auth, exactly like every other Space-scoped read.** ``require_space_role(MembershipRole.MEMBER)``
via ``ResourceCtx`` — literally the same dependency ``app.routers.resource_bookings`` builds
Resource-scoped routes on, imported rather than rebuilt, so a foreign Space or a foreign Resource
reads as 404 here exactly as it does on every booking route (``app.identity.authz``'s own module
docstring: a non-member must not be able to tell "does not exist" from "exists, not yours").

**Why this is per-Space now, not per-member.** The projection itself (task 1) evaluates only rule
types whose verdict is a pure function of the candidate interval and the date — ``reads_history ==
False`` — so it reads no member's booking history and no Resource's own bookings. Two different
members asking about the identical Space and date get the byte-identical rules-only answer; there is
no such thing as "this member's projection" independent of that. This is also what makes the answer
cacheable per Space and per date (``app.projection_cache``) rather than needing a cache entry per
member per Resource — see that module's own docstring for the whole shape of it.

**Overlap is applied after, never inside the scan.** A prior version of this endpoint synthesised an
``_ExistingBookingRule`` into the canon so an overlapping candidate came back denied by the engine.
That made the projection depend on one Resource's live bookings, which is exactly what defeats the
Space/date cache above — every Resource, and every booking made or cancelled, would need its own
cache entry. Instead the cached, Resource-independent rules-only answer is clipped afterward by
``app.projection.clip_overlap`` against this one Resource's own bookings for the window, by interval
arithmetic alone — no further ``evaluate_request`` call. See that function's own docstring for why
this is a data-layer fact rather than a rule verdict (``app/backend/app/db/driver.py``'s own
docstring: "double-booking a shared resource is an integrity invariant of the data layer, not a
configurable business rule"). The frontend's own ``CalendarGrid.tsx`` already fetches a Resource's
bookings itself and draws its own booking blocks and its own client-side overlap check
(``blockedReason``'s ``covering`` branch) — this endpoint is not wired to it yet (``POC.md``), but
when it is, this clip exists so *this* endpoint's own contract stays honest on its own — "bookable"
here must never mean "bookable, ignoring what is already booked" — without asking the client to do
that reconciliation a second time.

**Fail closed on an unbuildable row, per day.** ``rules_stub._build_canon`` raises
``_UnbuildableRuleRowError`` for a ``space_rules`` row whose type is unregistered or whose params no
longer satisfy it — ``rules_stub.evaluate()`` turns that into a denial for the one booking being
judged; this endpoint turns it into a denial for every candidate on that one day, via
``_AlwaysDenyRule(RULE_ERROR_MESSAGE)`` standing in for that day's whole canon. A day this happens on
projects as fully closed, not as unconstrained — the module docstring's "fail closed" carried one
level up from a single verdict to a whole day's worth of them. This is itself a fact about the
Space and the date, not about who is asking, so it is cached exactly like every other day.

**The window is capped.** ``MAX_BOOKABLE_DAYS`` bounds how many days one request may project, the
same spirit as ``app.identity.router.MAX_SCHEDULE_DAYS`` bounding ``GET /schedule`` — except this
endpoint's per-day cost, on a cache miss, is a brute-force scan (``app/backend/app/projection.py``'s
own module docstring), not a single resolution, so the cap here is much tighter: fourteen days is
comfortably past a two-week booking horizon check while keeping one request's worst case in the low
hundreds of milliseconds per the benchmark, not the seconds a 62-day window (``GET /schedule``'s own
cap) would cost run through this brute-force scan instead of that endpoint's O(1)-per-day resolution.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import BookingDriver
from app.db.session import get_session
from app.dependencies import get_driver
from app.identity import service
from app.identity.models import Resource
from app.projection import DayGrid, DayProjection, clip_overlap, project_days
from app.projection_cache import projection_cache
from app.rule_catalog import catalog
from app.routers.resource_bookings import ResourceBookingContext, resolve_resource
from app.rules_stub import (
    WEEK_STARTS_ON,
    NotProjectedRuleType,
    SpaceRuleConfig,
    _build_canon,
    _build_local_frame,
    _local_date,
    _local_midnight_utc,
    _resolve_run,
    _UnbuildableRuleRowError,
    projectable_config,
    resolve_day_schedule,
)
from rules import (
    RULE_ERROR_MESSAGE,
    BaseRule,
    CalendarContext,
    HistoryContext,
    UserContext,
)
from rules import BookingRequest as EngineBookingRequest
from rules import Context as EngineContext
from rules import RuleResult as EngineRuleResult

router = APIRouter(prefix="/spaces/{public_id}/resources/{resource_id}/bookable", tags=["bookable"])

SessionDep = Annotated[Session, Depends(get_session)]

#: How many days one request may project. Far tighter than ``MAX_SCHEDULE_DAYS`` (62) — see the
#: module docstring for why: a cache miss pays for a real ``evaluate_request`` call per candidate
#: length, not one O(1) resolution per day.
MAX_BOOKABLE_DAYS = 14

#: The grid's step when no ``session_length`` row governs a date at all. **A drawing resolution
#: only — it enforces nothing.** A date with no ``session_length`` row still runs a real projected
#: canon (``availability_hours``, ``max_duration``, whatever else is configured); this constant only
#: picks how finely the grid steps through candidate starts for *that* scan, and every slot's own
#: ``starts``/``reasons`` entry in the response is the server's actual, real answer for it — the
#: client colours the grid from those, never from this number. This is the opposite posture from the
#: frontend's own ``app/frontend/src/config.ts`` ``calendarConfig.sessionMinutes`` fallback, which
#: today stands in as if it were an enforced session length when none is configured; wiring the
#: calendar to this endpoint (a separate change, ``POC.md``) is what would let the client stop
#: needing a fallback that pretends to be enforcement at all.
UNCONFIGURED_DAY_DRAWING_RESOLUTION_MINUTES = 30

#: A local calendar day is 1440 minutes — mirrors ``app.projection._MINUTES_PER_LOCAL_DAY``, which
#: stays private to that module; restated here rather than imported so this file's own booking-window
#: clipping (``_booking_intervals_for_day``) does not reach into another module's underscore-prefixed
#: constant for one integer.
_MINUTES_PER_LOCAL_DAY = 1440

#: The projected canon (task 1's ``projectable_config``) never depends on which member or which
#: Resource is asking — every rule type it still holds has ``reads_history == False``. But
#: ``EngineBookingRequest``/``UserContext`` still require *some* string id to construct at all.
#: These are placeholders, not defaults with meaning: no rule this endpoint ever projects reads
#: either one, by construction — see ``_projection_context_builder``.
_PROJECTION_USER_ID = "projection"
_PROJECTION_RESOURCE_ID = "projection"

INVALID_WINDOW_DETAIL = "'to' must be strictly after 'from'"
WINDOW_TOO_WIDE_DETAIL = f"A single request may project at most {MAX_BOOKABLE_DAYS} days"

ALREADY_BOOKED_REASON_CODE = "already_booked"
ALREADY_BOOKED_MESSAGE = (
    "This time overlaps a booking that already exists on this Resource. Pick a different slot."
)


class _AlwaysDenyRule(BaseRule):
    """Stands in for a whole day's canon when that day's real one could not be built.

    See the module docstring, "Fail closed on an unbuildable row, per day" — this is what
    ``_canon_for_day`` returns instead of ``rules_stub._build_canon``'s own output whenever that
    raises ``_UnbuildableRuleRowError``, so the day projects as closed rather than as whatever a
    partially-assembled canon would have allowed.
    """

    def __init__(self, message: str) -> None:
        self._message = message

    def evaluate(self, request: EngineBookingRequest, context: EngineContext) -> EngineRuleResult:
        return EngineRuleResult.deny(self._message)


def _canon_for_day(config: SpaceRuleConfig) -> Callable[[date], tuple[BaseRule, ...]]:
    """The ``canon_for_day`` callable ``project_days`` calls once per projected date.

    ``config`` here is always the **already-filtered** config ``projectable_config`` returned —
    task 1's rules-only canon — never the Space's raw configuration; see the module docstring.
    """

    def build(on_date: date) -> tuple[BaseRule, ...]:
        try:
            return _build_canon(config, on_date)
        except _UnbuildableRuleRowError:
            return (_AlwaysDenyRule(RULE_ERROR_MESSAGE),)

    return build


def _projection_context_builder(
    config: SpaceRuleConfig, tz: ZoneInfo, now: datetime
) -> Callable[[datetime, datetime], tuple[EngineBookingRequest, EngineContext]]:
    """The ``make_request_and_context`` callable for the cached, rules-only scan.

    Builds a request and a context naming no real member and no real Resource — the projected
    canon never reads either, by construction: ``reads_history`` is what makes ``context.run`` or
    ``context.history`` matter to a rule at all, and every row ``projectable_config`` leaves in
    ``config`` has it ``False``. ``_resolve_run(request, (), timedelta(0))`` therefore always
    resolves to "the request alone, count 1" — the identical answer a real Space with empty history
    gets (``rules_stub._resolve_run``'s own docstring) — reused here rather than hand-built, so this
    path and a real per-booking evaluation share one implementation of what an empty-history run
    looks like rather than two that could drift.
    """

    def make_request_and_context(
        start_local: datetime, end_local: datetime
    ) -> tuple[EngineBookingRequest, EngineContext]:
        start_at = start_local.replace(tzinfo=tz).astimezone(timezone.utc)
        end_at = end_local.replace(tzinfo=tz).astimezone(timezone.utc)
        request = EngineBookingRequest(
            user_id=_PROJECTION_USER_ID,
            resource_id=_PROJECTION_RESOURCE_ID,
            start_at=start_at,
            end_at=end_at,
        )
        on_date = _local_date(start_at, config.timezone)
        local = _build_local_frame(request, config.timezone, on_date)
        run = _resolve_run(request, (), timedelta(0))
        context = EngineContext(
            user=UserContext(user_id=_PROJECTION_USER_ID),
            calendar=CalendarContext(week_starts_on=WEEK_STARTS_ON, now=now),
            local=local,
            run=run,
            history=HistoryContext(bookings=()),
        )
        return request, context

    return make_request_and_context


def _grid_for_date(config: SpaceRuleConfig, on_date: date) -> DayGrid:
    """The identical grid the calendar UI would draw for ``on_date``.

    Mirrors ``app/frontend/src/config.ts``'s own ``gridOffsetMinutes`` / ``slotsPerDayFor``: the
    grid spans the **whole local day**, anchored on ``anchor_minutes % slot_minutes`` rather than on
    ``anchor_minutes`` itself, so a venue opening off a whole session boundary (09:15 with hour-long
    sessions) still gets a grid whose rows land on the opening time rather than reporting it
    misconfigured — see that file's own module docstring for the reasoning this reuses rather than
    re-derives. ``resolve_day_schedule`` is the same per-date resolution ``GET
    /spaces/{public_id}/schedule`` already serves the frontend; this function performs the identical
    arithmetic the frontend performs on that response, once, in Python, so the window this endpoint
    projects is the window a human looking at the calendar would actually see.

    Takes the Space's **unfiltered** ``config`` — grid geometry is a ``session_length``/
    ``availability_hours`` concern, and neither type is ever excluded by task 1's filtering (both
    have ``reads_history == False``), so resolving against the filtered or the unfiltered config
    agrees; the unfiltered one is used so this function's meaning does not quietly depend on task
    1's own filtering staying in sync with it.
    """
    schedule = resolve_day_schedule(config, on_date)
    slot_minutes = schedule.session_minutes or UNCONFIGURED_DAY_DRAWING_RESOLUTION_MINUTES
    anchor_minutes = schedule.anchor_minutes if schedule.anchor_minutes is not None else 0
    grid_offset = anchor_minutes % slot_minutes
    slot_count = (_MINUTES_PER_LOCAL_DAY - grid_offset) // slot_minutes
    return DayGrid(
        date=on_date,
        slot_minutes=slot_minutes,
        first_slot_minutes=grid_offset,
        slot_count=slot_count,
    )


def _projected_day(
    grid: DayGrid,
    *,
    canon_for_day: Callable[[date], tuple[BaseRule, ...]],
    context_builder: Callable[[datetime, datetime], tuple[EngineBookingRequest, EngineContext]],
    space_id: int,
    rules_version: int,
    not_projected: tuple[NotProjectedRuleType, ...],
) -> DayProjection:
    """One day's rules-only projection, from the cache when possible, freshly scanned otherwise.

    ``catalog.generation`` is read fresh on every call, not hoisted to a caller-side local — this
    function may run after ``catalog.lookup`` has self-healed a reload mid-request (a miss inside
    ``_build_canon`` on an unrecognised generated type), and a stale generation read here would
    write a fresh scan into the cache under the *old* key, immediately shadowed by the version the
    next request would actually compute against. See ``app.projection_cache``'s own docstring for
    what this key means and why it is coarse.
    """
    catalog_generation = catalog.generation

    cached = projection_cache.get(
        space_id=space_id,
        on_date=grid.date,
        rules_version=rules_version,
        catalog_generation=catalog_generation,
    )
    if cached is not None:
        day, _ = cached
        return day

    (day,) = project_days(
        canon_for_day=canon_for_day,
        make_request_and_context=context_builder,
        days=(grid,),
        early_stop=True,
    )
    projection_cache.put(
        space_id=space_id,
        on_date=grid.date,
        rules_version=rules_version,
        catalog_generation=catalog_generation,
        day=day,
        not_projected=not_projected,
    )
    return day


def _booking_intervals_for_day(
    bookings, on_date: date, tz_name: str
) -> tuple[tuple[float, float], ...]:
    """One Resource's bookings, reduced to ``(start_minutes, end_minutes)`` pairs from ``on_date``'s
    own local midnight — the vocabulary ``app.projection.clip_overlap`` expects.

    Floors the start and ceilings the end, mirroring ``rules_stub._build_local_frame``'s own
    identical convention: rounding a booking's end down (or its start up) would report it as
    occupying less of the day than it really does, which is the permissive direction this codebase
    does not accept for a data-layer fact. A booking that does not touch this local day at all
    (``end_minutes <= 0`` or ``start_minutes >= 1440``) is dropped rather than clamped into a
    zero-width interval that would compare as "at" the boundary and clip a slot it never actually
    reached.
    """
    midnight = _local_midnight_utc(on_date, tz_name)
    intervals: list[tuple[float, float]] = []
    for booking in bookings:
        start_minutes = math.floor((booking.start_at - midnight).total_seconds() / 60)
        end_minutes = math.ceil((booking.end_at - midnight).total_seconds() / 60)
        if end_minutes <= 0 or start_minutes >= _MINUTES_PER_LOCAL_DAY:
            continue
        intervals.append((start_minutes, end_minutes))
    return tuple(intervals)


def _reason_runs(day: DayProjection) -> list[dict]:
    """Group ``day``'s contiguous fully-denied slots into one entry per run.

    A run is contiguous **grid slot indices**, not minutes, sharing the identical ``reason_code`` —
    two adjoining denied slots with *different* reasons are two runs, never merged into one whose
    text would describe only the first. ``toSlot`` is **exclusive**, matching every half-open bound
    elsewhere in this codebase (``CLAUDE.md``) rather than inventing an inclusive convention just for
    this endpoint.
    """
    reasons: list[dict] = []
    run_start: int | None = None
    run_code: str | None = None
    run_text: str | None = None

    def _close(end_index: int) -> None:
        if run_start is not None:
            reasons.append(
                {"fromSlot": run_start, "toSlot": end_index, "code": run_code, "text": run_text}
            )

    for index, slot in enumerate(day.slots):
        if slot.min_slots == 0:
            if run_start is None:
                run_start, run_code, run_text = index, slot.reason_code, slot.reason_text
            elif slot.reason_code != run_code:
                _close(index)
                run_start, run_code, run_text = index, slot.reason_code, slot.reason_text
        else:
            _close(index)
            run_start = None
    _close(len(day.slots))

    return reasons


def _day_to_wire(day: DayProjection) -> dict:
    return {
        "date": day.date.isoformat(),
        "slotMinutes": day.slot_minutes,
        "firstSlotMinutes": day.first_slot_minutes,
        "starts": [[slot.min_slots, slot.max_slots] for slot in day.slots],
        "reasons": _reason_runs(day),
    }


@router.get("", response_model=None)
def get_bookable(
    context: Annotated[ResourceBookingContext, Depends(resolve_resource)],
    driver: Annotated[BookingDriver, Depends(get_driver)],
    session: SessionDep,
    from_date: Annotated[date, Query(alias="from")],
    to_date: Annotated[date, Query(alias="to")],
) -> dict:
    """What may be booked on this Resource, ``[from, to)`` — identical for every member.

    See the module docstring for the whole shape of what this does and does not reuse. The
    response:

        {"slotMinutes": 30, "notProjected": [{"ruleType": "max_bookings_per_week",
                                                "label": "Max bookings per week"}],
         "days": [{"date": "2026-08-17", "slotMinutes": 30, "firstSlotMinutes": 0,
                    "starts": [[1, 4], [0, 0], ...],
                    "reasons": [{"fromSlot": 0, "toSlot": 17, "code": "...", "text": "..."}]}]}

    ``starts[i]`` is ``[min_slots, max_slots]`` for the grid slot starting at
    ``day.firstSlotMinutes + i * day.slotMinutes`` — see ``app.projection.SlotProjection`` for
    exactly what those two numbers mean, including why ``max_slots`` is the top of the *contiguous*
    run of allowed lengths rather than the longest allowed length found anywhere. ``[0, 0]`` means
    nothing may be booked from that start; the reason is reported once per contiguous run in
    ``reasons``, not once per slot.

    ``notProjected`` names every rule type this Space has configured that the projection never
    evaluates at all (task 1) — a history-reading type, always enforced at booking time but never
    drawn here. A caller (a calendar) that shows this grid as the whole story without also showing
    this list is telling a member less than it knows.

    Each day carries its **own** ``slotMinutes`` — a Space whose ``session_length`` is scoped by
    ``applies_to`` to particular weekdays can genuinely resolve a different slot size on different
    days in the same window, and this is what makes that visible per day rather than reporting one
    number for the whole response. The **top-level** ``slotMinutes`` is not a second source of
    truth for any one day — it is the finest (smallest) ``slotMinutes`` across every projected day,
    the same "shared axis granularity" concept ``app/frontend/src/config.ts``'s
    ``finestSessionMinutes`` already uses for a heterogeneous week: a caller that wants one number
    to lay out a shared time axis across every day has one, and a caller that wants each day's own
    real answer reads ``days[i].slotMinutes`` instead.
    """
    space = context.space_context.space
    resource: Resource = context.resource

    if to_date <= from_date:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_WINDOW_DETAIL)
    if (to_date - from_date).days > MAX_BOOKABLE_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=WINDOW_TOO_WIDE_DETAIL)

    config = service.space_rule_config(session, space)
    tz = ZoneInfo(config.timezone)
    now = datetime.now(timezone.utc)

    # Task 1: only the rules-only config ever reaches the scan. `not_projected` is the same list
    # for every day in the window — exclusion is decided by rule *type*, never by date — so it is
    # computed once here rather than inside the per-day cache path.
    projected_config, not_projected = projectable_config(config)
    canon_for_day = _canon_for_day(projected_config)
    context_builder = _projection_context_builder(projected_config, tz, now)

    grids = tuple(
        _grid_for_date(config, from_date + timedelta(days=offset))
        for offset in range((to_date - from_date).days)
    )

    rules_version = space.rules_version
    projections = tuple(
        _projected_day(
            grid,
            canon_for_day=canon_for_day,
            context_builder=context_builder,
            space_id=space.id,
            rules_version=rules_version,
            not_projected=not_projected,
        )
        for grid in grids
    )

    # Task 2: overlap is a fact about this one Resource, applied after the Space/date-scoped
    # projection above rather than folded into it — see the module docstring.
    window_start_utc = _local_midnight_utc(from_date, config.timezone)
    window_end_utc = _local_midnight_utc(to_date, config.timezone)
    existing = driver.list_bookings(
        start=window_start_utc, end=window_end_utc, resource_id=resource.id, include_cancelled=False
    )

    days = tuple(
        clip_overlap(
            day,
            _booking_intervals_for_day(existing, day.date, config.timezone),
            reason_code=ALREADY_BOOKED_REASON_CODE,
            reason_text=ALREADY_BOOKED_MESSAGE,
        )
        for day in projections
    )

    return {
        "slotMinutes": (
            min(day.slot_minutes for day in days)
            if days
            else UNCONFIGURED_DAY_DRAWING_RESOLUTION_MINUTES
        ),
        "notProjected": [
            {"ruleType": entry.rule_type, "label": entry.label} for entry in not_projected
        ],
        "days": [_day_to_wire(day) for day in days],
    }
