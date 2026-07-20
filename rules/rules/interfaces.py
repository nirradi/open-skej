"""Core data interfaces for the Open-Skej rule engine.

Every rule in the canon is written against exactly these types. They are frozen, validated at
construction, and contain **no rule logic** — a rule that needs to decide something decides it in
its own ``evaluate``, never here.

Two invariants are enforced everywhere and are worth stating once:

* **UTC only.** Every ``datetime`` crossing this boundary must be timezone-aware with a zero UTC
  offset. Naive datetimes are rejected rather than assumed to be UTC, because a silently-assumed
  timezone is the single most likely source of subtle rule bugs. Timezone is a presentation concern
  owned by the UI.
* **History is pre-filtered.** ``HistoryContext`` contains exactly the bookings that count. The
  engine never inspects booking status — ``BookingRecord`` has no status field — so if a booking
  should not count toward a limit, the caller does not put it in the context.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum

__all__ = [
    "Weekday",
    "BookingRecord",
    "BookingRequest",
    "UserContext",
    "CalendarContext",
    "HistoryContext",
    "Context",
    "RuleResult",
    "BaseRule",
    "history_window",
    "HISTORY_ROLLING_WINDOW",
]

#: How far either side of "now" the rolling history window reaches. ``HistoryContext`` is capped at
#: the current calendar month **or** this rolling window, whichever is wider.
HISTORY_ROLLING_WINDOW = timedelta(days=7)


class Weekday(IntEnum):
    """Days of the week, numbered to match :meth:`datetime.date.weekday`."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


def _require_utc(value: object, label: str) -> datetime:
    """Return ``value`` if it is a UTC-aware datetime, else raise ``ValueError``/``TypeError``."""
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be a datetime, got {type(value).__name__}")
    offset = value.utcoffset()
    if offset is None:
        raise ValueError(
            f"{label} must be timezone-aware UTC; got naive datetime {value!r}. "
            "The rule engine never assumes a timezone."
        )
    if offset != timedelta(0):
        raise ValueError(f"{label} must be UTC (zero offset); got offset {offset} in {value!r}")
    return value


def _validate_span(start_at: object, end_at: object, label: str) -> None:
    """Validate a half-open ``[start_at, end_at)`` interval of UTC datetimes."""
    start = _require_utc(start_at, f"{label}.start_at")
    end = _require_utc(end_at, f"{label}.end_at")
    if start >= end:
        raise ValueError(f"{label}.start_at must be strictly before end_at; got {start} >= {end}")


@dataclass(frozen=True)
class BookingRecord:
    """A booking that already exists. Carries **no status** — see the module docstring."""

    user_id: str
    resource_id: str
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        _validate_span(self.start_at, self.end_at, "BookingRecord")


@dataclass(frozen=True)
class BookingRequest:
    """A booking a user is asking for. Not yet persisted, not yet approved."""

    user_id: str
    resource_id: str
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        _validate_span(self.start_at, self.end_at, "BookingRequest")

    @property
    def duration(self) -> timedelta:
        return self.end_at - self.start_at


@dataclass(frozen=True)
class UserContext:
    """Who is asking.

    ``user_id`` only, deliberately. Role and tier belong to Stream 2 and no rule in this phase
    branches on either; a field nothing reads is a field that will be wrong by the time something
    does. Add them when a rule needs them.
    """

    user_id: str


@dataclass(frozen=True)
class CalendarContext:
    """Calendar conventions and the current instant.

    No timezone field: everything is UTC, so week boundaries are UTC boundaries and the engine has
    no DST cases at all.
    """

    week_starts_on: Weekday
    now: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.week_starts_on, Weekday):
            raise TypeError(
                "CalendarContext.week_starts_on must be a Weekday, "
                f"got {type(self.week_starts_on).__name__}"
            )
        _require_utc(self.now, "CalendarContext.now")


@dataclass(frozen=True)
class HistoryContext:
    """The user's relevant prior bookings for this resource.

    Already capped and already filtered by the caller. Everything in here counts.
    """

    bookings: tuple[BookingRecord, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.bookings, (str, bytes)) or not hasattr(self.bookings, "__iter__"):
            raise TypeError(
                f"HistoryContext.bookings must be an iterable of BookingRecord, "
                f"got {type(self.bookings).__name__}"
            )
        bookings = tuple(self.bookings)
        for index, booking in enumerate(bookings):
            if not isinstance(booking, BookingRecord):
                raise TypeError(
                    f"HistoryContext.bookings[{index}] must be a BookingRecord, "
                    f"got {type(booking).__name__}"
                )
        object.__setattr__(self, "bookings", bookings)

    def __len__(self) -> int:
        return len(self.bookings)


def _month_start(moment: datetime) -> datetime:
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(moment: datetime) -> datetime:
    start = _month_start(moment)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def history_window(now: datetime) -> tuple[datetime, datetime]:
    """Return the half-open ``[lower, upper)`` bound history may draw from at ``now``.

    The current calendar month **or** a rolling week either side of ``now``, whichever is wider.
    """
    now = _require_utc(now, "now")
    lower = min(_month_start(now), now - HISTORY_ROLLING_WINDOW)
    upper = max(_next_month_start(now), now + HISTORY_ROLLING_WINDOW)
    return lower, upper


@dataclass(frozen=True)
class Context:
    """Everything a rule is allowed to know, aggregated into one object.

    Aggregating rather than passing four positional parameters is what lets a later task add a new
    kind of context without breaking the ``evaluate`` signature of every rule ever written.

    The history window invariant is enforced here rather than on ``HistoryContext`` because it is
    the only place both the history and ``now`` are visible. A rule must not be able to silently
    rely on history it will not be given in production.
    """

    user: UserContext
    calendar: CalendarContext
    history: HistoryContext = field(default_factory=HistoryContext)

    def __post_init__(self) -> None:
        if not isinstance(self.user, UserContext):
            raise TypeError(f"Context.user must be a UserContext, got {type(self.user).__name__}")
        if not isinstance(self.calendar, CalendarContext):
            raise TypeError(
                f"Context.calendar must be a CalendarContext, got {type(self.calendar).__name__}"
            )
        if not isinstance(self.history, HistoryContext):
            raise TypeError(
                f"Context.history must be a HistoryContext, got {type(self.history).__name__}"
            )

        lower, upper = history_window(self.calendar.now)
        for index, booking in enumerate(self.history.bookings):
            # Overlap, not containment: a booking straddling the boundary is still in window.
            if booking.end_at <= lower or booking.start_at >= upper:
                raise ValueError(
                    f"Context.history.bookings[{index}] falls outside the permitted history "
                    f"window [{lower}, {upper}): {booking.start_at} - {booking.end_at}. "
                    "The caller must cap history before building the context."
                )

    @property
    def now(self) -> datetime:
        """The current instant, in UTC. Shorthand for ``context.calendar.now``."""
        return self.calendar.now


@dataclass(frozen=True)
class RuleResult:
    """The verdict of a single rule.

    ``fail_reason`` is **user-facing copy**, shown verbatim in the UI — never an exception repr,
    never a rule class name. ``passed=True`` implies ``fail_reason is None``.

    (The stream brief calls this field ``pass``; that is a Python keyword and cannot be a field
    name, hence ``passed``.)
    """

    passed: bool
    fail_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise TypeError(f"RuleResult.passed must be a bool, got {type(self.passed).__name__}")
        if self.passed:
            if self.fail_reason is not None:
                raise ValueError(
                    f"RuleResult.passed=True must have fail_reason=None; got {self.fail_reason!r}"
                )
        else:
            if self.fail_reason is None:
                raise ValueError("RuleResult.passed=False must supply a user-facing fail_reason")
            if not isinstance(self.fail_reason, str):
                raise TypeError(
                    f"RuleResult.fail_reason must be a str, got {type(self.fail_reason).__name__}"
                )
            if not self.fail_reason.strip():
                raise ValueError("RuleResult.fail_reason must not be blank")

    @classmethod
    def allow(cls) -> "RuleResult":
        return cls(passed=True)

    @classmethod
    def deny(cls, fail_reason: str) -> "RuleResult":
        return cls(passed=False, fail_reason=fail_reason)


class BaseRule(ABC):
    """The contract every rule in the canon implements.

    Rules are classes, not functions, so their parameters live on the instance: the canon holds
    ``MaxDurationRule(max_duration=...)``, and per-Space configuration later becomes a change to
    how the canon is built rather than a change to any rule.
    """

    @abstractmethod
    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        """Decide whether ``request`` is permitted. Must not mutate its arguments."""

    @property
    def name(self) -> str:
        """Identifier for logs and test output. Never shown to users."""
        return type(self).__name__
