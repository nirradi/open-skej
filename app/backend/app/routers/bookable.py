"""``GET /spaces/{public_id}/resources/{resource_id}/bookable`` — the projection, live.

A read-only window onto ``app.projection.project_days`` run against a **real** Space: its real
``space_rules`` canon (assembled per day, task 1's ``canon_for_day``), the real caller's real
booking history, and the Resource's real existing bookings — not the synthetic, in-memory Space
``scripts/projection_bench.py`` measures against. Nothing here reimplements a rule's semantics or
the adapter's own local-time resolution; every piece is the same machinery
``app.routers.resource_bookings`` and ``GET /spaces/{public_id}/schedule`` already call, reused
rather than re-derived (module docstrings on ``app/backend/app/rules_stub.py`` and
``app/backend/app/projection.py`` both make this the load-bearing rule for this whole area of the
codebase, and this endpoint is not the place to start breaking it).

**Auth, exactly like every other Space-scoped read.** ``require_space_role(MembershipRole.MEMBER)``
via ``ResourceCtx`` — literally the same dependency ``app.routers.resource_bookings`` builds
Resource-scoped routes on, imported rather than rebuilt, so a foreign Space or a foreign Resource
reads as 404 here exactly as it does on every booking route (``app.identity.authz``'s own module
docstring: a non-member must not be able to tell "does not exist" from "exists, not yours").

**Why this is per-member and not per-Space.** ``config.reads_history`` can be true (a
``max_bookings_per_week`` row, ``max_consecutive_duration``, any counting rule at all), and history
is always *this user's own* prior bookings across the Space (``rules_stub._load_space_history``'s
own docstring: "This user's confirmed bookings"). Two different members can legitimately see two
different projections of the identical window. There is no such thing as "the Space's projection"
independent of who is asking, so this endpoint always projects for ``ResourceCtx``'s own
authenticated caller — never a caller-supplied user id, which would turn this into a way to read
someone else's booking eligibility.

**Real bookability, not just real rules.** ``rules.evaluate_request`` knows nothing about whether a
slot is already taken — that is a data-layer invariant enforced by ``BookingDriver.create_booking``
raising ``OverlapError``, not a canon rule (``app/backend/app/db/driver.py``'s own docstring:
"double-booking a shared resource is an integrity invariant of the data layer, not a configurable
business rule"). A projection that only ran the canon would report an already-booked slot as
bookable, which is exactly the "renders as everything is bookable" failure this whole POC exists to
avoid rendering. So ``_ExistingBookingRule`` — a synthetic rule that exists only in this module, not
a ``space_rules`` row and not part of any real Space's canon — is appended to the end of every day's
resolved canon, closing over the Resource's confirmed bookings for the whole requested window
(loaded once via ``driver.list_bookings``, not once per candidate). It runs last: every real
configured rule's own denial message still wins when more than one thing would refuse a slot, and
"this time is already booked" only surfaces when nothing else does.

**Fail closed on an unbuildable row, per day.** ``rules_stub._build_canon`` raises
``_UnbuildableRuleRowError`` for a ``space_rules`` row whose type is unregistered or whose params no
longer satisfy it — ``rules_stub.evaluate()`` turns that into a denial for the one booking being
judged; this endpoint turns it into a denial for every candidate on that one day, via
``_AlwaysDenyRule(RULE_ERROR_MESSAGE)`` standing in for that day's whole canon. A day this happens on
projects as fully closed, not as unconstrained — the module docstring's "fail closed" carried one
level up from a single verdict to a whole day's worth of them.

**The window is capped.** ``MAX_BOOKABLE_DAYS`` bounds how many days one request may project, the
same spirit as ``app.identity.router.MAX_SCHEDULE_DAYS`` bounding ``GET /schedule`` — except this
endpoint's per-day cost is a brute-force scan (``app/backend/app/projection.py``'s own module
docstring), not a single resolution, so the cap here is much tighter: fourteen days is comfortably
past a two-week booking horizon check while keeping one request's worst case in the low hundreds of
milliseconds per the benchmark, not the seconds a 62-day window (``GET /schedule``'s own cap) would
cost run through this brute-force scan instead of that endpoint's O(1)-per-day resolution.
"""

from __future__ import annotations

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
from app.projection import DayGrid, DayProjection, project_days
from app.routers.resource_bookings import ResourceBookingContext, _load_space_history, resolve_resource
from app.rules_stub import (
    WEEK_STARTS_ON,
    SpaceRuleConfig,
    _build_canon,
    _build_local_frame,
    _engine_record,
    _local_date,
    _local_midnight_utc,
    _resolve_run,
    _UnbuildableRuleRowError,
    resolve_day_schedule,
)
from rules import (
    RULE_ERROR_MESSAGE,
    BaseRule,
    CalendarContext,
    HistoryContext,
    UserContext,
)
from rules import BookingRecord as EngineBookingRecord
from rules import BookingRequest as EngineBookingRequest
from rules import Context as EngineContext
from rules import RuleResult as EngineRuleResult

router = APIRouter(prefix="/spaces/{public_id}/resources/{resource_id}/bookable", tags=["bookable"])

SessionDep = Annotated[Session, Depends(get_session)]

#: How many days one request may project. Far tighter than ``MAX_SCHEDULE_DAYS`` (62) — see the
#: module docstring for why: this scan pays for a real ``evaluate_request`` call per candidate
#: length, not one O(1) resolution per day.
MAX_BOOKABLE_DAYS = 14

#: The grid's step when no ``session_length`` row governs a date at all — mirrors the frontend's own
#: fallback (``app/frontend/src/config.ts``'s ``calendarConfig.sessionMinutes``), so an unconfigured
#: date projects against the identical grid a human would see drawn, not a different guess at one.
DEFAULT_SLOT_MINUTES = 30

INVALID_WINDOW_DETAIL = "'to' must be strictly after 'from'"
WINDOW_TOO_WIDE_DETAIL = f"A single request may project at most {MAX_BOOKABLE_DAYS} days"

ALREADY_BOOKED_MESSAGE = (
    "This time overlaps a booking that already exists on this Resource. Pick a different slot."
)


class _AlwaysDenyRule(BaseRule):
    """Stands in for a whole day's canon when that day's real one could not be built.

    See the module docstring, "Fail closed on an unbuildable row, per day" — this is what
    ``canon_for_day`` returns instead of ``rules_stub._build_canon``'s own output whenever that
    raises ``_UnbuildableRuleRowError``, so the day projects as closed rather than as whatever a
    partially-assembled canon would have allowed.
    """

    def __init__(self, message: str) -> None:
        self._message = message

    def evaluate(self, request: EngineBookingRequest, context: EngineContext) -> EngineRuleResult:
        return EngineRuleResult.deny(self._message)


class _ExistingBookingRule(BaseRule):
    """Denies any candidate overlapping a Resource's own already-confirmed booking.

    Not a ``space_rules`` row and not part of any real Space's canon — see the module docstring,
    "Real bookability, not just real rules". Closes over the Resource's bookings for the whole
    projected window, loaded once by the endpoint handler, not once per candidate.
    """

    def __init__(self, bookings: tuple[EngineBookingRecord, ...]) -> None:
        self._bookings = bookings

    def evaluate(self, request: EngineBookingRequest, context: EngineContext) -> EngineRuleResult:
        for booking in self._bookings:
            if request.start_at < booking.end_at and booking.start_at < request.end_at:
                return EngineRuleResult.deny(ALREADY_BOOKED_MESSAGE)
        return EngineRuleResult.allow()


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
    """
    schedule = resolve_day_schedule(config, on_date)
    slot_minutes = schedule.session_minutes or DEFAULT_SLOT_MINUTES
    anchor_minutes = schedule.anchor_minutes if schedule.anchor_minutes is not None else 0
    grid_offset = anchor_minutes % slot_minutes
    slot_count = (1440 - grid_offset) // slot_minutes
    return DayGrid(
        date=on_date, slot_minutes=slot_minutes, first_slot_minutes=grid_offset, slot_count=slot_count
    )


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
    """What the authenticated caller may book on this Resource, ``[from, to)``.

    See the module docstring for the whole shape of what this does and does not reuse. The
    response:

        {"slotMinutes": 30, "days": [{"date": "2026-08-17", "firstSlotMinutes": 0,
                                       "starts": [[1, 4], [0, 0], ...],
                                       "reasons": [{"fromSlot": 0, "toSlot": 17,
                                                     "code": "...", "text": "..."}]}]}

    ``starts[i]`` is ``[min_slots, max_slots]`` for the grid slot starting at
    ``firstSlotMinutes + i * slotMinutes`` — see ``app.projection.SlotProjection`` for exactly what
    those two numbers mean, including why ``max_slots`` is the top of the *contiguous* run of
    allowed lengths rather than the longest allowed length found anywhere. ``[0, 0]`` means nothing
    may be booked from that start; the reason is reported once per contiguous run in ``reasons``,
    not once per slot.

    ``slotMinutes`` is reported once, at the top level, taken from the **first** projected day's own
    resolved grid. This is a real POC simplification, not an oversight: a Space whose canon scopes a
    ``session_length`` row to particular weekdays via ``applies_to`` can legitimately resolve a
    different slot size on a different day in the same window (task 1's whole reason for existing),
    and this envelope has no way to say that — each ``DayProjection`` does carry its own
    ``slot_minutes`` internally, but the wire shape above, as specified, does not surface it per day.
    A production version would move ``slotMinutes`` inside each day object; see ``POC.md``.
    """
    space = context.space_context.space
    resource: Resource = context.resource
    user = context.user

    if to_date <= from_date:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_WINDOW_DETAIL)
    if (to_date - from_date).days > MAX_BOOKABLE_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=WINDOW_TOO_WIDE_DETAIL)

    config = service.space_rule_config(session, space)
    tz = ZoneInfo(config.timezone)
    now = datetime.now(timezone.utc)

    # History: this member's own confirmed bookings across the Space, loaded once for the whole
    # window — never per candidate — and only when the canon would actually read it (the same
    # "no wasted round trip" property `app.routers.resource_bookings.create_resource_booking`
    # already documents for the identical query).
    history = _load_space_history(session, space, user.id, now=now) if config.reads_history else ()
    history_records = tuple(_engine_record(entry) for entry in history)

    # The Resource's own existing bookings over the requested window, loaded once — see the module
    # docstring, "Real bookability, not just real rules". The same `_local_midnight_utc` the adapter
    # itself uses for every local-day bound, not a second implementation of "local midnight in UTC".
    window_start_utc = _local_midnight_utc(from_date, config.timezone)
    window_end_utc = _local_midnight_utc(to_date, config.timezone)
    existing = driver.list_bookings(
        start=window_start_utc, end=window_end_utc, resource_id=resource.id, include_cancelled=False
    )
    existing_records = tuple(
        EngineBookingRecord(
            user_id=str(booking.user_id),
            resource_id=str(booking.resource_id),
            start_at=booking.start_at,
            end_at=booking.end_at,
        )
        for booking in existing
    )
    overlap_rule = _ExistingBookingRule(existing_records)

    def canon_for_day(on_date: date) -> tuple[BaseRule, ...]:
        try:
            canon = _build_canon(config, on_date)
        except _UnbuildableRuleRowError:
            return (_AlwaysDenyRule(RULE_ERROR_MESSAGE),)
        return canon + (overlap_rule,)

    def make_request_and_context(
        start_local: datetime, end_local: datetime
    ) -> tuple[EngineBookingRequest, EngineContext]:
        start_at = start_local.replace(tzinfo=tz).astimezone(timezone.utc)
        end_at = end_local.replace(tzinfo=tz).astimezone(timezone.utc)
        request = EngineBookingRequest(
            user_id=str(user.id), resource_id=str(resource.id), start_at=start_at, end_at=end_at
        )
        on_date = _local_date(start_at, config.timezone)
        local = _build_local_frame(request, config.timezone, on_date)
        tolerance_minutes = resolve_day_schedule(config, on_date).session_minutes or 0
        run = _resolve_run(request, history_records, timedelta(minutes=tolerance_minutes))
        engine_context = EngineContext(
            user=UserContext(user_id=str(user.id)),
            calendar=CalendarContext(week_starts_on=WEEK_STARTS_ON, now=now),
            local=local,
            run=run,
            history=HistoryContext(bookings=history_records),
        )
        return request, engine_context

    grids = tuple(
        _grid_for_date(config, from_date + timedelta(days=offset))
        for offset in range((to_date - from_date).days)
    )

    projections = project_days(
        canon_for_day=canon_for_day,
        make_request_and_context=make_request_and_context,
        days=grids,
        early_stop=True,
    )

    return {
        "slotMinutes": projections[0].slot_minutes if projections else DEFAULT_SLOT_MINUTES,
        "days": [_day_to_wire(day) for day in projections],
    }
