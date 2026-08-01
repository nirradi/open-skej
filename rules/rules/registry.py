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
* **``reads_history``** — whether the constructed rule consults ``context.history`` at all, so a
  caller can skip loading a Space's booking history when nothing configured would read it.
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
  intended pattern, not a mistake — ``availability_hours`` and ``max_duration`` are both meant to
  vary by day (e.g. "Mon/Wed/Fri 10–15" and "Tue/Thu 8–12" as two separate ``availability_hours``
  rows), so a second instance of either warrants no warning at all.
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

from .canon import AvailabilityHoursRule, BookingHorizonRule, MaxDurationRule, NotInThePastRule
from .frequency import MaxBookingsPerMonthRule, MaxBookingsPerWeekRule
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

    Only two values, because only two are needed by the six types registered below: a plain positive
    integer (a day count, a minute count, a booking count) and a local wall-clock time of day (an
    opening or closing hour). A ``str`` subclass so a future API boundary serialises this as the
    plain string a form or a JSON body already expects, without a translation table.
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

    ``minimum`` is the only bound these six types need: every integer parameter registered below
    must be positive (a zero-day horizon or a zero-minute duration means nothing), and a local time
    of day has no numeric bound to speak of. Nothing here declares a maximum or a step, because
    nothing registered needs one — add a bound against the type that actually requires it.
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
    """Everything a registered rule type declares about itself. See the module docstring."""

    rule_type: str
    label: str
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


def _build_availability_hours(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None
) -> BaseRule:
    # `AvailabilityHoursRule` only ever speaks UTC. `params` holds the Space's raw local wall-clock
    # hours as authored; `resolved` holds the UTC pair a future adapter resolves per booking date —
    # the constructor takes the latter, never the former.
    assert resolved is not None
    return AvailabilityHoursRule(opens_at=resolved["opens_at"], closes_at=resolved["closes_at"])


def _build_max_bookings_per_week(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None
) -> BaseRule:
    assert resolved is not None
    return MaxBookingsPerWeekRule(
        max_bookings=params["max_bookings"],
        window_start=resolved["window_start"],
        window_end=resolved["window_end"],
    )


def _build_max_bookings_per_month(
    params: Mapping[str, Any], resolved: Mapping[str, Any] | None
) -> BaseRule:
    assert resolved is not None
    return MaxBookingsPerMonthRule(
        max_bookings=params["max_bookings"],
        window_start=resolved["window_start"],
        window_end=resolved["window_end"],
    )


# --- the starter registry --------------------------------------------------------------------
#
# The six predicates already in force today (`rule-engine.md`, "The canon"). Priority reproduces
# that section's documented assembled order exactly, spaced in multiples of 10 rather than
# consecutive integers: a later `SlotAlignmentRule` sits between `MaxDurationRule` and
# `AvailabilityHoursRule` (its priority "sits beside MaxDurationRule" per the plan this registry
# comes from), and consecutive integers would force renumbering everything to make room for it.

_RULE_TYPES: tuple[RuleType, ...] = (
    RuleType(
        rule_type="not_in_the_past",
        label="Not in the past",
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
        rule_type="availability_hours",
        label="Availability hours",
        priority=40,
        params=(
            RuleParam(
                name="opens_at",
                kind=ParamKind.LOCAL_TIME,
                label="Opens at",
                unit=None,
                required=True,
            ),
            RuleParam(
                name="closes_at",
                kind=ParamKind.LOCAL_TIME,
                label="Closes at",
                unit=None,
                required=True,
            ),
        ),
        reads_history=False,
        needs_local_resolution=True,
        is_single=False,
        build=_build_availability_hours,
    ),
    RuleType(
        rule_type="max_bookings_per_week",
        label="Max bookings per week",
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
