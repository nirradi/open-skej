"""The rule type registry: what a rule type declares about itself.

Every rule the API can configure today is a hand-written Python class an adapter names by import —
``rules_stub._build_canon`` is a hardcoded if-chain over seven ``spaces`` columns, and adding a rule
type means editing that chain, the schema, and a form. This module is the alternative: a rule type
declares its own identity, parameter schema, ordering and storage needs as data, so a future
adapter (6.6), API (6.7) and admin form (6.8) can all read the same declaration instead of each
hand-coding a third of it.

**A registered type declares, once:**

* a **stable string id** — never the Python class name. A future ``space_rules.rule_type`` column
  stores this id on a running venue's configuration; renaming the class it happens to be implemented
  by must not silently orphan every row that named it.
* a **label** and a **description** — the description is prose for an admin choosing a rule, "what
  it refuses", written for someone who will never read the Python. Hand-written for each of the
  eight types below; for a generated type it is authored by the model in a manifest call made after
  the generation loop verifies the rule (``rules/generation/manifest.py``), never the admin's own
  prompt.
* an **ordered parameter schema** (``RuleParam``) — rich enough to render a form field and to
  validate a request body's params against, because those are the same five facts
  (``kind``/``label``/``unit``/``required``/``minimum``) read by two different callers. One schema
  serving both jobs is what keeps a form and its validator from drifting into disagreeing about a
  parameter, which would surface as a form that submits a 422.
* a **priority** — an integer, not a position in a list. **Rule order comes from declared priority,
  never from row order or insertion order.** The canon this stream's adapter assembles is sorted by
  it (then by row id, for two instances of the same type); nothing about ordering is
  admin-authored, because the order arbitrates which denial message a user sees (see "The canon"
  below), and that is a product decision made once per rule *type*, not a drag handle handed to
  whichever admin configures a Space last.
* **``reads_history``** — whether this rule type's **verdict** depends on history, so a caller can
  skip loading a Space's booking history when nothing configured would read it. This is *not* the
  same test as "does ``evaluate``'s own source mention ``context.history``" —
  ``max_consecutive_duration`` is the type that pulls the two apart: its ``evaluate`` reads only
  ``context.run``, but the run itself is resolved from history by the adapter before the rule ever
  sees it (``RunContext``, ``.claude/rules/rule-engine.md``), so a Space configuring this rule and
  nothing else that reads history *does* need the Space-wide history query run — without it the
  run the rule receives is always the request alone and it silently never denies, the exact
  silently-permissive failure this codebase refuses. ``reads_history`` is declared with that
  meaning in mind for every type below, hand-written or generated: "a caller must supply history
  for this rule's verdict to be correct," not "grep the source for the word history."
* **``needs_local_resolution``** — whether the constructor needs values resolved against the Space's
  own timezone and the booking's own date, rather than the raw stored params. This is what keeps
  every local-to-UTC conversion at the adapter boundary instead of inviting a rule type to convert
  for itself — the same discipline ``CalendarContext``'s missing timezone field enforces on the
  engine itself.
* **``is_single``** — advisory only, never a uniqueness constraint. True says a second instance of
  the type is probably a mistake worth a warning, not a state the storage layer refuses to
  represent — the engine's flat AND makes two instances of the same type coherent (they AND to
  the stricter), which is coherent for ``max_bookings_per_week`` but rarely what anyone meant. False
  says the opposite: multiple instances scoped to different days or dates via ``applies_to`` are the
  intended pattern, not a mistake — ``max_duration`` is meant to vary by day (e.g. a tighter cap on
  a busy evening than on a quiet Sunday morning via two day-scoped rows), so a second instance of it
  warrants no warning at all. ``max_consecutive_duration`` is ``is_single=False`` for the identical
  reason.
* a **build function** from validated params (and, for a type with ``needs_local_resolution``, a
  second mapping of resolved values) to a constructed instance of the rule it names.

**This registry does not replace ``DEFAULT_CANON``.** ``canon.default_canon()`` keeps meaning what
it already means — the reference assembly of the four hand-written rules at their default values,
which the generation loop is measured against — and nothing here reads it or feeds it. The registry
is an additive, separate description of the same rule *classes*, for a different purpose: driving a
form and a per-Space canon assembly that a later task builds, not this one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any

from .canon import (
    BookingHorizonRule,
    MaxConsecutiveDurationRule,
    MaxDurationRule,
    NotInThePastRule,
)
from .frequency import (
    MaxBookingsPerDayRule,
    MaxBookingsPerMonthRule,
    MaxBookingsPerWeekRule,
    MaxDurationPerDayRule,
)
from .interfaces import BaseRule

__all__ = [
    "ParamKind",
    "RuleParam",
    "RuleType",
    "REGISTRY",
    "rule_types",
]


class ParamKind(str, Enum):
    """The shape of one declared parameter.

    Two values: a plain positive integer (a day count, a minute count, a booking count) and a local
    time of day presented as a clock but **stored as an integer** — minutes from local midnight.
    ``LOCAL_TIME`` describes *presentation*, not storage: the value underneath it is exactly as much
    an ``int`` as ``INTEGER``'s is, which is what would let a form render a time-of-day widget
    (09:00) while the wire and the database hold the plain minute count (540). No type currently
    registered below declares a ``LOCAL_TIME`` parameter — ``availability_hours``, the last one
    that did, is retired, its opening-hours question now answered by the calendar shape
    (``rules/shape/``, ``.claude/rules/calendar-shape.md``) instead of a rule. The kind stays
    registered because a future generated type's manifest may still declare one. A ``str`` subclass
    so a future API boundary serialises this as the plain string a form or a JSON body already
    expects, without a translation table.
    """

    INTEGER = "integer"
    LOCAL_TIME = "local_time"


@dataclass(frozen=True)
class RuleParam:
    """One parameter a rule type's ``build`` function reads, described as data.

    This is the constructor's contract, not a copy of it: a future admin form (6.8) reads ``kind``,
    ``label``, ``unit`` and ``required`` to render a field, and a future request validator (6.7)
    reads ``required`` and ``minimum`` to accept or reject a submitted value — before either of
    those exists. One schema serves both, because two independently written ones would drift, and
    the drift would show up as a form whose own submission gets refused.

    ``minimum`` is the only bound these eight types need: every integer parameter registered below
    must be positive (a zero-day horizon or a zero-minute duration means nothing). Nothing here
    declares a maximum or a step, because nothing registered needs one *as a single-field bound* —
    add one against the type that actually requires it.
    """

    name: str
    kind: ParamKind
    label: str
    unit: str | None
    required: bool
    minimum: int | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("RuleParam.name must not be empty")
        if not isinstance(self.kind, ParamKind):
            raise TypeError(f"RuleParam.kind must be a ParamKind, got {type(self.kind).__name__}")
        if not self.label:
            raise ValueError("RuleParam.label must not be empty")


#: A rule type's constructor: validated ``params`` plus, for a type with ``needs_local_resolution``,
#: a mapping of values already resolved against the Space's zone and the booking's own date.
#: ``resolved`` is ``None`` for a type that does not need it — the three date/duration types below
#: never read their second argument.
BuildFn = Callable[[Mapping[str, Any], "Mapping[str, Any] | None"], BaseRule]


@dataclass(frozen=True)
class RuleType:
    """Everything a registered rule type declares about itself. See the module docstring.

    ``description`` is prose for an admin choosing a rule, never for a developer reading the
    source — "what it refuses", in a sentence or two. Hand-written for each of the eight types
    below; for a generated type it is authored by the model in a manifest call made after the
    generation loop verifies the rule (``rules/generation/manifest.py``), never the admin's own
    prompt, and validated non-empty the same way ``label`` and ``rule_type`` already are: a picker
    where some entries explain themselves and others do not is worse than one where none do.
    """

    rule_type: str
    label: str
    description: str
    priority: int
    params: tuple[RuleParam, ...]
    reads_history: bool
    needs_local_resolution: bool
    is_single: bool
    build: BuildFn

    def __post_init__(self) -> None:
        if not self.rule_type:
            raise ValueError("RuleType.rule_type must not be empty")
        if not self.label:
            raise ValueError("RuleType.label must not be empty")
        if not self.description:
            raise ValueError("RuleType.description must not be empty")
        if not isinstance(self.priority, int):
            raise TypeError(f"RuleType.priority must be an int, got {type(self.priority).__name__}")
        if not callable(self.build):
            raise TypeError("RuleType.build must be callable")
        for index, param in enumerate(self.params):
            if not isinstance(param, RuleParam):
                raise TypeError(
                    f"RuleType.params[{index}] must be a RuleParam, got {type(param).__name__}"
                )


# --- build functions ------------------------------------------------------------------------
#
# Each mirrors its rule class's own constructor exactly (read canon.py / frequency.py before
# changing one) — this module adds no behaviour of its own, only the data that names it.


def _build_not_in_the_past(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None = None
) -> BaseRule:
    return NotInThePastRule()


def _build_booking_horizon(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None = None
) -> BaseRule:
    return BookingHorizonRule(days=params["days"])


def _build_max_duration(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None = None
) -> BaseRule:
    # Mirrors the `spaces.max_duration_minutes` column's own unit — minutes, not a raw `timedelta`,
    # since params must stay JSON-serialisable for a future JSONB column.
    return MaxDurationRule(max_duration=timedelta(minutes=params["max_duration_minutes"]))


def _build_max_consecutive_duration(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None = None
) -> BaseRule:
    # Mirrors `_build_max_duration` exactly, bar the param name: the run's minutes, not the
    # request's own.
    return MaxConsecutiveDurationRule(
        max_duration=timedelta(minutes=params["max_consecutive_minutes"])
    )


def _build_max_bookings_per_week(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None
) -> BaseRule:
    assert resolved is not None
    # `tolerance` (task 8.6): the gap `evaluate` closes when it merges the request with history
    # into sessions, resolved by the adapter exactly as `app.rules_stub._resolve_run` resolves it
    # for `context.run` — see `rules.frequency`'s module docstring for why the merge happens in
    # the rule rather than on `Context.history` itself.
    return MaxBookingsPerWeekRule(
        max_bookings=params["max_bookings"],
        window_start=resolved["window_start"],
        window_end=resolved["window_end"],
        tolerance=resolved["tolerance"],
    )


def _build_max_bookings_per_month(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None
) -> BaseRule:
    assert resolved is not None
    return MaxBookingsPerMonthRule(
        max_bookings=params["max_bookings"],
        window_start=resolved["window_start"],
        window_end=resolved["window_end"],
        tolerance=resolved["tolerance"],
    )


def _build_max_bookings_per_day(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None
) -> BaseRule:
    assert resolved is not None
    # Task 8.7's counting sibling of the week/month build functions above — the identical
    # `tolerance` resolution, just from `app.rules_stub._local_day_bounds` instead of
    # `_local_week_bounds` / `_local_month_bounds`.
    return MaxBookingsPerDayRule(
        max_bookings=params["max_bookings"],
        window_start=resolved["window_start"],
        window_end=resolved["window_end"],
        tolerance=resolved["tolerance"],
    )


def _build_max_duration_per_day(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None
) -> BaseRule:
    assert resolved is not None
    # Unlike the three counting build functions above, no `tolerance`: `MaxDurationPerDayRule`
    # sums raw history entries rather than merging them into runs, so it takes no gap to close
    # (`rules.frequency`'s module docstring, and the rule's own).
    return MaxDurationPerDayRule(
        max_duration=timedelta(minutes=params["max_duration_minutes"]),
        window_start=resolved["window_start"],
        window_end=resolved["window_end"],
    )


# --- the starter registry --------------------------------------------------------------------
#
# The eight predicates in force today (`rule-engine.md`, "The canon" and "`frequency.py`"). Priority
# reproduces that section's documented assembled order exactly, spaced in multiples of 10 (plus
# deliberate multiple-of-5 insertions) rather than consecutive integers, so a later type can still
# be inserted between two existing ones without renumbering the rest.
#
# `session_length` and `availability_hours` are retired (task 10.5) — the calendar shape now says
# what they said — but their priorities (35 and 40) are deliberately not reassigned to anything
# else: the gaps they leave are exactly what let them have landed there in the first place, and
# reusing a freed number would only reintroduce the renumbering risk this spacing exists to avoid.
# `max_consecutive_duration` sits at 32, strictly between `max_duration` (30) and the 35 gap:
# a booking that breaks both duration rules at once is more usefully told to shorten itself
# (fixable by editing only this request) than to stop abutting a neighbour (fixable only by
# touching a booking that already exists), so `max_duration` keeps first refusal.
# `max_duration_per_day` (42) and `max_bookings_per_day` (45) sit strictly between the 40 gap
# and `max_bookings_per_week` (50) — task 8.7's own reasoning: of the three windows a user could
# break a cap in at once (day/week/month), the narrowest is the most useful thing to be told, so
# the day-scoped pair comes before the week and month rules; between the two, the duration total
# precedes the booking count for the same reason `max_duration` precedes `max_consecutive_duration`
# above — a total is judged before a count when both could fire on the same day.

_RULE_TYPES: tuple[RuleType, ...] = (
    RuleType(
        rule_type="not_in_the_past",
        label="Not in the past",
        description="Refuses a booking that starts in the past.",
        priority=10,
        params=(),
        reads_history=False,
        needs_local_resolution=False,
        is_single=True,
        build=_build_not_in_the_past,
    ),
    RuleType(
        rule_type="booking_horizon",
        label="Booking horizon",
        description="Refuses a booking made too far in advance — more than the configured "
        "number of days from now.",
        priority=20,
        params=(
            RuleParam(
                name="days",
                kind=ParamKind.INTEGER,
                label="Days ahead",
                unit="days",
                required=True,
                minimum=1,
            ),
        ),
        reads_history=False,
        needs_local_resolution=False,
        is_single=True,
        build=_build_booking_horizon,
    ),
    RuleType(
        rule_type="max_duration",
        label="Maximum duration",
        description="Refuses a single booking that is itself longer than the configured maximum "
        "— its own start-to-end span, checked on its own, however far apart the member's other "
        "bookings are. For a cap on back-to-back play across more than one booking, use Maximum "
        "consecutive duration instead.",
        priority=30,
        params=(
            RuleParam(
                name="max_duration_minutes",
                kind=ParamKind.INTEGER,
                label="Maximum duration",
                unit="minutes",
                required=True,
                minimum=1,
            ),
        ),
        reads_history=False,
        needs_local_resolution=False,
        is_single=False,
        build=_build_max_duration,
    ),
    RuleType(
        rule_type="max_consecutive_duration",
        label="Maximum consecutive duration",
        description="Refuses a booking that would push a run of back-to-back bookings by the same "
        "member — this one plus every booking it directly abuts, on any Resource in the Space — "
        "past the configured maximum. Two one-hour bookings held one after another count as one "
        "two-hour run; a gap between them keeps the two separate. For a cap on a single booking's "
        "own length regardless of what it joins, use Maximum duration instead.",
        priority=32,
        params=(
            RuleParam(
                name="max_consecutive_minutes",
                kind=ParamKind.INTEGER,
                label="Maximum consecutive duration",
                unit="minutes",
                required=True,
                minimum=1,
            ),
        ),
        reads_history=True,
        needs_local_resolution=False,
        is_single=False,
        build=_build_max_consecutive_duration,
    ),
    RuleType(
        rule_type="max_duration_per_day",
        label="Max duration per day",
        description="Refuses a booking once a member's total booked time in that local day, this "
        "booking included, would exceed the configured duration — counted across every Resource "
        "in the Space. Unlike Maximum consecutive duration, this sums every booking that day "
        "rather than only a contiguous run, so two separate bookings with a gap between them "
        "still count toward it.",
        priority=42,
        params=(
            RuleParam(
                name="max_duration_minutes",
                kind=ParamKind.INTEGER,
                label="Max duration per day",
                unit="minutes",
                required=True,
                minimum=1,
            ),
        ),
        reads_history=True,
        needs_local_resolution=True,
        is_single=True,
        build=_build_max_duration_per_day,
    ),
    RuleType(
        rule_type="max_bookings_per_day",
        label="Max bookings per day",
        description="Refuses a booking once a member already holds the configured number of "
        "bookings in that local day, counted across every Resource in the Space.",
        priority=45,
        params=(
            RuleParam(
                name="max_bookings",
                kind=ParamKind.INTEGER,
                label="Max bookings",
                unit="bookings",
                required=True,
                minimum=1,
            ),
        ),
        reads_history=True,
        needs_local_resolution=True,
        is_single=True,
        build=_build_max_bookings_per_day,
    ),
    RuleType(
        rule_type="max_bookings_per_week",
        label="Max bookings per week",
        description="Refuses a booking once a member already holds the configured number of "
        "bookings in that week, counted across every Resource in the Space.",
        priority=50,
        params=(
            RuleParam(
                name="max_bookings",
                kind=ParamKind.INTEGER,
                label="Max bookings",
                unit="bookings",
                required=True,
                minimum=1,
            ),
        ),
        reads_history=True,
        needs_local_resolution=True,
        is_single=True,
        build=_build_max_bookings_per_week,
    ),
    RuleType(
        rule_type="max_bookings_per_month",
        label="Max bookings per month",
        description="Refuses a booking once a member already holds the configured number of "
        "bookings in that calendar month, counted across every Resource in the Space.",
        priority=60,
        params=(
            RuleParam(
                name="max_bookings",
                kind=ParamKind.INTEGER,
                label="Max bookings",
                unit="bookings",
                required=True,
                minimum=1,
            ),
        ),
        reads_history=True,
        needs_local_resolution=True,
        is_single=True,
        build=_build_max_bookings_per_month,
    ),
)

#: Keyed on the stable string id — never the Python class name. See the module docstring.
REGISTRY: dict[str, RuleType] = {rule_type.rule_type: rule_type for rule_type in _RULE_TYPES}


def rule_types() -> tuple[RuleType, ...]:
    """Every registered type, sorted by declared priority — the order an assembled canon runs in."""
    return tuple(sorted(REGISTRY.values(), key=lambda rule_type: rule_type.priority))
