"""Turns a :class:`~shape.types.Shape` plus one local date into the intervals a booking may occupy.

Two functions, and the second is defined in terms of the first:

* :func:`project_day` resolves everything the grid needs to draw one local date: the operating
  intervals in force, the blackout intervals in force, the exact set of grid-aligned starts a
  booking may begin at with what duration each offers, and whether the date is bookable at all.
* :func:`permits` is the enforcement question a booking gate asks -- does ``[start_minutes,
  end_minutes)`` fit -- answered by looking the request up in exactly the table :func:`project_day`
  already built, so the two can never quietly disagree about what is offered.

**Fail closed.** This is what the booking gate consults (OVERVIEW decision 8, ``project_day``
before ``evaluate``), so its failure modes are booking failure modes. A shape that fails to project
a date, a request :func:`permits` cannot make sense of -- every one of those is **no booking**,
never "open by default". :func:`permits` itself never raises for an ordinary refusal; it raises
only for a genuine caller mistake -- an inverted interval, a naive ``datetime`` where a local
``date`` was asked for -- the identical split ``rules.controller`` draws between a denial
(``RuleResult.deny``) and ``ContextMismatchError``: a denial is member-facing copy, and presenting a
caller bug as a polite refusal would hide the bug instead of surfacing it. The engine's own
statement of this in ``.claude/rules/rule-engine.md`` ("Fail closed -- non-negotiable") is the
register every message here is written to match, including never printing an absolute clock time or
date in denial copy -- a minute count has no zone to be wrong in, but a rendered "18:00" silently
claims one.

**The grid is chunked forward from each operating block's own ``start_time``, never from local
midnight** -- the reasoning ``SessionLengthRule`` already gives ("the anchor is the date's own
resolved opening time"), reproduced here per block rather than per Space, since a shape's blocks can
each open at a different time of day. **The step is the block's own smallest offered duration**,
``min(allowed_durations_mins)`` -- the teacher and the lab each declare one duration, where the step
and the only duration are the same number; the music room's evening block declares two, and the
worked acceptance case (a 120-minute booking is offered starting 15:00, one hour after the block
opens) only holds if the grid is stepped by the smaller duration and every declared duration is then
checked independently for fit at each such start, not if 120 needed its own multiple-of-120 grid
from the same anchor. A block's own grid is entirely independent of any other block's: two
overlapping blocks each keep their own anchor and step, and a start reachable from either grid is
offered -- this is what "the allowed durations at an instant are the union of the overlapping
blocks' lists" (decision 5) means at the level of an actual candidate start rather than only as a
description of the structural interval.

**A blackout truncates, not shifts.** A candidate ``[start, start + duration)`` that overlaps any
blackout window in force on that date is simply not offered; the grid keeps stepping and the next
candidate the grid itself produces is offered normally if nothing else rules it out. There is no
special-cased "resume right after the blackout ends" logic and none is needed: the teacher's 19:40
slot is already on the 18:00-anchored 20-minute grid (18:00, 18:20, 18:40, 19:00, 19:20, 19:40), so
removing 19:20 for overlapping the 19:30-19:40 break leaves 19:40 exactly where the grid always put
it. A residual -- the 10 minutes of 19:30-19:40 left over after 19:20 is refused -- is never offered
on its own, because it was never a candidate: only durations the block itself declares are ever
checked, and 10 is not one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .types import BlackoutWindow, OperatingBlock, Shape

__all__ = [
    "OfferedStart",
    "OperatingInterval",
    "BlackoutInterval",
    "DayProjection",
    "ShapeVerdict",
    "InvalidBookingRequestError",
    "project_day",
    "permits",
]


class InvalidBookingRequestError(ValueError):
    """The caller asked :func:`permits` a question it cannot make sense of.

    Raised only for a caller mistake -- an inverted or non-positive interval, a ``datetime`` handed
    where a local ``date`` was asked for -- never for an ordinary refusal, which is a
    :class:`ShapeVerdict` with ``allowed=False``. The same split ``rules.controller`` draws between
    ``ContextMismatchError`` and a denial: this is loud on the cause and fails closed on the outcome
    either way, since a caller that lets this propagate makes no booking either.
    """


@dataclass(frozen=True)
class OfferedStart:
    """One grid-aligned local minute a booking may begin at, and every duration offered there."""

    start_minutes: int
    durations_mins: tuple[int, ...]


@dataclass(frozen=True)
class OperatingInterval:
    """One merged region of operating time in force on a date.

    Two source blocks are merged into one interval only when they genuinely **overlap**
    (``start < previous.end``, strict) -- never merely touch. Two blocks sharing a boundary, like
    the music room's 08:00-14:00 and 14:00-22:00, stay two separate intervals with two separate
    grids; merging a shared boundary would make the evening block's wider duration list look
    reachable one tick before it actually opens, which decision 5's union rule was never meant to
    grant. Where two intervals do overlap, ``allowed_durations_mins`` is their union -- a structural
    summary of what is nominally offered across the merged region, not itself grid- or
    blackout-aware; the exact, per-minute answer is :class:`OfferedStart`.
    """

    start_minutes: int
    end_minutes: int
    allowed_durations_mins: tuple[int, ...]


@dataclass(frozen=True)
class BlackoutInterval:
    """One blackout window in force on a date, with its own reason."""

    start_minutes: int
    end_minutes: int
    reason: str


@dataclass(frozen=True)
class DayProjection:
    """Everything the grid needs to draw one local date, resolved and ready to render.

    ``offered_starts`` is sorted by ``start_minutes`` and is the authoritative table
    :func:`permits` is answered from -- a start/duration pair is permitted exactly when it appears
    here, nowhere else. ``bookable`` is ``bool(offered_starts)``, named separately because "is there
    anything to draw at all" is the first question a calendar view asks about a date, before it asks
    what.
    """

    on_date: date
    operating_intervals: tuple[OperatingInterval, ...]
    blackout_intervals: tuple[BlackoutInterval, ...]
    offered_starts: tuple[OfferedStart, ...]
    bookable: bool


@dataclass(frozen=True)
class ShapeVerdict:
    """The answer to "may this booking occupy this interval?", and why not if it may not.

    ``reason`` is member-facing copy, shown verbatim -- never an absolute clock time, calendar date,
    or timezone name, for the identical reason ``.claude/rules/rule-engine.md`` gives for the
    engine's own denial copy: nothing in this package knows the venue's zone, so a printed "18:00"
    would silently claim one it does not have. ``allowed=True`` implies ``reason=None``.
    """

    allowed: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise TypeError(
                f"ShapeVerdict.allowed must be a bool, got {type(self.allowed).__name__}"
            )
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError(
                f"ShapeVerdict.reason must be a str or None, got {type(self.reason).__name__}"
            )
        if self.allowed and self.reason is not None:
            raise ValueError(
                f"ShapeVerdict.allowed=True must have reason=None; got {self.reason!r}"
            )


def _require_local_date(value: object, label: str) -> date:
    if isinstance(value, datetime):
        raise InvalidBookingRequestError(
            f"{label} must be a plain date, not a datetime; got {value!r}. A shape question is "
            "about a local calendar date; a datetime silently carries a time of day this package "
            "would have to guess how to discard."
        )
    if not isinstance(value, date):
        raise InvalidBookingRequestError(f"{label} must be a date, got {type(value).__name__}")
    return value


def _require_minutes(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidBookingRequestError(f"{label} must be an int, got {type(value).__name__}")
    return value


def _overlaps(start: int, end: int, other_start: int, other_end: int) -> bool:
    """Whether half-open ``[start, end)`` overlaps half-open ``[other_start, other_end)``."""
    return start < other_end and other_start < end


def _active_blocks(shape: Shape, on_date: date) -> tuple[OperatingBlock, ...]:
    return tuple(block for block in shape.operating_blocks if block.applies_on(on_date))


def _active_blackouts(shape: Shape, on_date: date) -> tuple[BlackoutWindow, ...]:
    return tuple(window for window in shape.blackout_windows if window.applies_on(on_date))


def _blackout_intervals(blackouts: tuple[BlackoutWindow, ...]) -> tuple[BlackoutInterval, ...]:
    intervals = [
        BlackoutInterval(window.start_time, window.end_time, window.reason) for window in blackouts
    ]
    intervals.sort(key=lambda interval: interval.start_minutes)
    return tuple(intervals)


def _operating_intervals(blocks: tuple[OperatingBlock, ...]) -> tuple[OperatingInterval, ...]:
    """Merge ``blocks`` into the fewest intervals, joining only where two blocks genuinely overlap.

    See :class:`OperatingInterval` for why a shared boundary does not merge.
    """
    if not blocks:
        return ()

    ordered = sorted(blocks, key=lambda block: (block.start_time, block.end_time))
    merged: list[list] = []  # each entry: [start, end, set-of-durations]
    for block in ordered:
        if merged and block.start_time < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], block.end_time)
            merged[-1][2] |= set(block.allowed_durations_mins)
        else:
            merged.append([block.start_time, block.end_time, set(block.allowed_durations_mins)])

    return tuple(
        OperatingInterval(start, end, tuple(sorted(durations))) for start, end, durations in merged
    )


def _offered_starts(
    blocks: tuple[OperatingBlock, ...], blackouts: tuple[BlackoutInterval, ...]
) -> tuple[OfferedStart, ...]:
    """Sweep every block's own grid, independently, and union what lands on the same minute.

    Each block contributes candidates on its own ``start_time``-anchored grid, stepped by its own
    smallest declared duration -- see the module docstring. A candidate is offered a given duration
    only when the booking it would produce both fits inside that block's own window and clears every
    blackout in force; a block never grants a duration another block declares.
    """
    by_start: dict[int, set[int]] = {}
    for block in blocks:
        step = min(block.allowed_durations_mins)
        candidate = block.start_time
        while candidate + step <= block.end_time:
            for duration in block.allowed_durations_mins:
                end = candidate + duration
                if end > block.end_time:
                    continue
                if any(
                    _overlaps(candidate, end, bo.start_minutes, bo.end_minutes) for bo in blackouts
                ):
                    continue
                by_start.setdefault(candidate, set()).add(duration)
            candidate += step

    return tuple(
        OfferedStart(start, tuple(sorted(durations)))
        for start, durations in sorted(by_start.items())
    )


def project_day(shape: Shape, on_date: date) -> DayProjection:
    """Resolve ``shape`` against one local ``on_date``.

    Never raises for any valid ``Shape`` and any local ``date`` -- a date no block or blackout
    applies to simply projects to an unbookable day, which is the correct answer for it, not a
    failure. Raises :class:`InvalidBookingRequestError` only if ``on_date`` is not a plain local
    date (a ``datetime`` included -- see :func:`permits`'s own note on that).
    """
    on_date = _require_local_date(on_date, "on_date")

    blocks = _active_blocks(shape, on_date)
    blackouts = _active_blackouts(shape, on_date)

    blackout_intervals = _blackout_intervals(blackouts)
    operating_intervals = _operating_intervals(blocks)
    offered_starts = _offered_starts(blocks, blackout_intervals)

    return DayProjection(
        on_date=on_date,
        operating_intervals=operating_intervals,
        blackout_intervals=blackout_intervals,
        offered_starts=offered_starts,
        bookable=bool(offered_starts),
    )


def permits(shape: Shape, on_date: date, start_minutes: int, end_minutes: int) -> ShapeVerdict:
    """Whether a booking spanning local ``[start_minutes, end_minutes)`` on ``on_date`` is offered.

    Answered by looking ``start_minutes`` and the implied duration up in
    :func:`project_day`'s own ``offered_starts`` table -- the identical table the calendar grid and
    the shape preview render from, so the gate can never grant something the grid never drew or
    refuse something the grid offered. When refused, the diagnosis below is independent
    presentation logic layered on top of that same lookup, in the order the task's own contract
    states: no operating block covers the span, then a blackout overlap, then a duration this
    Space does not offer, then (having ruled out all three) a start that simply does not land on
    any covering block's own grid.

    Raises :class:`InvalidBookingRequestError` for a caller mistake -- a non-int minute count, an
    inverted or empty interval, a ``datetime`` in place of ``on_date`` -- never for an ordinary
    refusal. See the module docstring's "Fail closed" paragraph for why the two are kept apart.
    """
    on_date = _require_local_date(on_date, "on_date")
    start = _require_minutes(start_minutes, "start_minutes")
    end = _require_minutes(end_minutes, "end_minutes")
    if start < 0:
        raise InvalidBookingRequestError(f"start_minutes must be non-negative; got {start}")
    if end <= start:
        raise InvalidBookingRequestError(
            f"end_minutes must be strictly after start_minutes; got {end} <= {start}"
        )

    projection = project_day(shape, on_date)
    duration = end - start

    for offered in projection.offered_starts:
        if offered.start_minutes == start and duration in offered.durations_mins:
            return ShapeVerdict(allowed=True)

    return ShapeVerdict(allowed=False, reason=_diagnose(shape, on_date, start, end))


def _diagnose(shape: Shape, on_date: date, start: int, end: int) -> str:
    """Build member-facing copy for a refusal already established by :func:`permits`.

    Never prints an absolute clock time, calendar date, or the venue's zone -- see the module
    docstring. A block's ``reason`` for a blackout is the one piece of admin-authored text that is
    safe to include verbatim; it is prose the admin wrote to be read, not a value this package
    derived and could get the zone of wrong.
    """
    blocks = _active_blocks(shape, on_date)
    blackouts = _blackout_intervals(_active_blackouts(shape, on_date))

    covering = [block for block in blocks if block.start_time <= start and end <= block.end_time]
    if not covering:
        return "This isn't during a time we're open. Check the calendar for our hours."

    for blackout in blackouts:
        if _overlaps(start, end, blackout.start_minutes, blackout.end_minutes):
            return f"That overlaps a closure: {blackout.reason}."

    allowed_durations = sorted({d for block in covering for d in block.allowed_durations_mins})
    duration = end - start
    if duration not in allowed_durations:
        return "Bookings here don't come in that length. Check the calendar for what's offered."

    return "That start time isn't one we offer. Check the calendar for the available times."
