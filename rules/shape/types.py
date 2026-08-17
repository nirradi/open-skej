"""The calendar shape document's types.

A shape is **data describing a venue's week**, never executable code and never a predicate over who
is asking -- see ``.claude/rules/calendar-shape.md`` for the line between a shape and a rule. These
dataclasses are the parsed, already-valid form of the document:
:func:`shape.validate.validate_shape`
is the only supported way to obtain one from untrusted JSON, and it produces the precise,
field-path-qualified error an admin's chat turn is retried against
(``operating_blocks[1].allowed_durations_mins: must not be empty``). The ``__post_init__`` checks
below are a second, independent line -- the same belt-and-suspenders posture
``rules.interfaces`` takes for the engine's own contract types -- guarding a caller that constructs
one of these directly rather than through the validator; they raise with a short, class-scoped
message, not a JSON path, because they have no path to report.

**Every clock value here is local wall-clock minutes, and every date is a local calendar date.** A
shape carries no timezone at all (OVERVIEW decision 7) -- the Space's own zone converts at the
boundary, exactly as ``.claude/rules/rule-engine.md`` already draws that line for the engine. A
``start_time`` of ``540`` means 09:00 in whatever zone the venue is eventually resolved against, and
nothing in this module ever asks which one that is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = [
    "DAY_CODES",
    "DAY_NAMES",
    "OperatingBlock",
    "BlackoutWindow",
    "Shape",
]

#: The three-letter day codes the JSON document spells, mapped onto ``datetime.date.weekday()``'s
#: own numbering (0 = Monday) so a parsed day integer is directly comparable to
#: ``on_date.weekday()`` with no second translation table anywhere downstream.
DAY_CODES = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}

#: The reverse of ``DAY_CODES``, for rendering a weekday integer back into the document's own
#: spelling -- error messages and any later UI both want this direction too.
DAY_NAMES = {value: key for key, value in DAY_CODES.items()}

#: A local wall-clock minute must describe a single day's own start: 0 (00:00) through 1439 (23:59).
_MAX_START_MINUTES = 1439

#: An interval's own local day, in minutes: the ceiling ``end_time`` may reach past ``start_time``.
_DAY_MINUTES = 1440


def _check_time_range(start_time: int, end_time: int, label: str) -> None:
    """Enforce the one time-range bound this whole document shares.

    Identical to ``AvailabilityHoursRule``'s own constructor bound
    (``.claude/rules/rule-engine.md``, "``closes_at_minutes`` may exceed 1440"): a window is at
    most 24 hours long and always starts on the day it is configured for, so ``end_time`` may run
    past local midnight -- representing a venue open past midnight, decision 2 -- but never further
    than one full day past its own start. An inversion is a typo a caller can produce; a value past
    this ceiling is not a meaning this document can express at all, so both are rejected here
    rather than read into.
    """
    if not (0 <= start_time <= _MAX_START_MINUTES):
        raise ValueError(
            f"{label}.start_time must be within a single local day (0 <= start_time < 1440); "
            f"got {start_time!r}"
        )
    if not (start_time < end_time <= start_time + _DAY_MINUTES):
        raise ValueError(
            f"{label}.end_time must be after start_time and at most 24h later; got "
            f"start_time={start_time!r}, end_time={end_time!r}"
        )


def _check_effective_range(
    effective_from: date | None, effective_to: date | None, label: str
) -> None:
    if effective_from is not None and effective_to is not None and effective_from > effective_to:
        raise ValueError(
            f"{label}.effective_to must not be before effective_from; got "
            f"effective_from={effective_from!r}, effective_to={effective_to!r}"
        )


@dataclass(frozen=True)
class OperatingBlock:
    """A recurring window in which the venue offers time, and the durations it offers there.

    ``days`` is **required and non-empty** -- unlike a blackout, an operating block that named no
    day would offer nothing, which is what an empty ``operating_blocks`` list already says; there
    is no second way to say it. ``allowed_durations_mins`` is likewise required and non-empty,
    ascending, and free of duplicates -- the ascending order is not merely tidy,
    :mod:`shape.projection` reads ``min(allowed_durations_mins)`` as this block's own grid step
    (see that module's docstring), so a caller cannot construct a block whose first-listed duration
    silently disagrees with its smallest.

    ``effective_from`` / ``effective_to`` are **inclusive on both ends**, which is the one place
    this schema breaks with the engine's half-open convention on purpose. They are calendar dates a
    human typed into a chat, not instants: "closed from June 1 to August 31" excluding the 31st is
    the bug every scheduling product ships once, and a half-open reading here would reproduce it
    silently.

    **Overlapping operating blocks are legal and are never rejected here** -- decision 5. Two blocks
    covering the same instant is how "1 hour in the morning, 1 or 2 hours in the evening" is
    expressed at all, and an admin refining a shape by chat will produce touching and overlapping
    blocks constantly. Resolving what an overlap means -- the union of the overlapping blocks' own
    durations, since each block is an independent *grant* of time rather than a constraint being
    ANDed -- is :mod:`shape.projection`'s job, not this type's; a block validates entirely on its
    own.
    """

    days: frozenset[int]
    start_time: int
    end_time: int
    allowed_durations_mins: tuple[int, ...]
    effective_from: date | None = None
    effective_to: date | None = None

    def __post_init__(self) -> None:
        if not self.days:
            raise ValueError("OperatingBlock.days must not be empty")
        for day in self.days:
            if isinstance(day, bool) or not isinstance(day, int) or day not in range(7):
                raise ValueError(f"OperatingBlock.days must contain weekday ints 0-6; got {day!r}")
        _check_time_range(self.start_time, self.end_time, "OperatingBlock")
        if not self.allowed_durations_mins:
            raise ValueError("OperatingBlock.allowed_durations_mins must not be empty")
        durations = list(self.allowed_durations_mins)
        if any(isinstance(d, bool) or not isinstance(d, int) or d < 1 for d in durations):
            raise ValueError(
                f"OperatingBlock.allowed_durations_mins must be ints >= 1; got {durations!r}"
            )
        if len(set(durations)) != len(durations) or durations != sorted(durations):
            raise ValueError(
                "OperatingBlock.allowed_durations_mins must be strictly ascending with no "
                f"duplicates; got {durations!r}"
            )
        _check_effective_range(self.effective_from, self.effective_to, "OperatingBlock")

    def applies_on(self, on_date: date) -> bool:
        """Whether this block governs ``on_date`` at all -- its effective range and its own days."""
        if self.effective_from is not None and on_date < self.effective_from:
            return False
        if self.effective_to is not None and on_date > self.effective_to:
            return False
        return on_date.weekday() in self.days


@dataclass(frozen=True)
class BlackoutWindow:
    """A recurring or one-off window in which the venue does **not** offer time, with a reason.

    ``days`` and ``date`` are mutually exclusive -- decision 3 -- and **neither** means "every day":
    ``date`` is the one-off ("closed this Friday"), ``days`` is the recurring break ("Mondays"),
    and a blackout naming neither applies every day within its own ``effective_from`` /
    ``effective_to`` range, which is exactly what an admin means by "closed for the whole season"
    without also having to spell out which days of the week that covers.

    ``effective_from`` / ``effective_to`` are inclusive on both ends, for the identical reason
    ``OperatingBlock`` gives.
    """

    start_time: int
    end_time: int
    reason: str
    days: frozenset[int] | None = None
    date: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None

    def __post_init__(self) -> None:
        _check_time_range(self.start_time, self.end_time, "BlackoutWindow")
        if not self.reason or not self.reason.strip():
            raise ValueError("BlackoutWindow.reason must not be empty")
        if self.days is not None and self.date is not None:
            raise ValueError("BlackoutWindow must not carry both 'days' and 'date'")
        if self.days is not None:
            if not self.days:
                raise ValueError("BlackoutWindow.days must not be empty when present")
            for day in self.days:
                if isinstance(day, bool) or not isinstance(day, int) or day not in range(7):
                    raise ValueError(
                        f"BlackoutWindow.days must contain weekday ints 0-6; got {day!r}"
                    )
        _check_effective_range(self.effective_from, self.effective_to, "BlackoutWindow")

    def applies_on(self, on_date: date) -> bool:
        """Whether this blackout is in force on ``on_date`` -- its effective range, then its own
        recurrence: a specific ``date``, a set of ``days``, or (naming neither) every day."""
        if self.effective_from is not None and on_date < self.effective_from:
            return False
        if self.effective_to is not None and on_date > self.effective_to:
            return False
        if self.date is not None:
            return on_date == self.date
        if self.days is not None:
            return on_date.weekday() in self.days
        return True


@dataclass(frozen=True)
class Shape:
    """The whole document: a version, and the operating blocks and blackouts it declares.

    An **empty** shape (``operating_blocks=()``, ``blackout_windows=()``) is a valid value, not a
    degenerate one -- it is what a Space told "closed until further notice" holds, and
    :func:`shape.projection.project_day` reports every date of it as unbookable rather than raising.
    """

    version: int
    operating_blocks: tuple[OperatingBlock, ...]
    blackout_windows: tuple[BlackoutWindow, ...]

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError(f"Shape.version must be an int >= 1; got {self.version!r}")
        blocks = tuple(self.operating_blocks)
        for index, block in enumerate(blocks):
            if not isinstance(block, OperatingBlock):
                raise TypeError(
                    f"Shape.operating_blocks[{index}] must be an OperatingBlock, "
                    f"got {type(block).__name__}"
                )
        object.__setattr__(self, "operating_blocks", blocks)

        blackouts = tuple(self.blackout_windows)
        for index, blackout in enumerate(blackouts):
            if not isinstance(blackout, BlackoutWindow):
                raise TypeError(
                    f"Shape.blackout_windows[{index}] must be a BlackoutWindow, "
                    f"got {type(blackout).__name__}"
                )
        object.__setattr__(self, "blackout_windows", blackouts)
