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
* **Local questions are pre-answered.** No type here carries a timezone, and none ever will. The
  caller is the only thing that knows the venue's zone, so it resolves the venue's day, week, month,
  weekday and time-of-day into ``LocalFrame`` — UTC instants and plain integers — before a rule is
  called. A rule reads the answer; it never converts.
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
    "LocalFrame",
    "RunContext",
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


def _require_int(value: object, label: str) -> int:
    """Return ``value`` if it is a plain ``int``, else raise ``TypeError``.

    ``bool`` is rejected explicitly: it is a subclass of ``int``, and ``True`` reaching a field that
    means "minutes past midnight" would be silently read as one minute past.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an int, got {type(value).__name__}")
    return value


def _validate_bounds(start: object, end: object, label: str) -> None:
    """Validate a half-open ``[start, end)`` pair of UTC instants."""
    lower = _require_utc(start, f"{label}_start")
    upper = _require_utc(end, f"{label}_end")
    if lower >= upper:
        raise ValueError(
            f"{label}_start must be strictly before {label}_end; got {lower} >= {upper}"
        )


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

    **No timezone field, and there is never going to be one.** A zone here would be readable by
    every rule in the canon, generated ones above all, and a rule that converts for itself is a rule
    whose correctness depends on it having picked the same zone the caller meant. ``LocalFrame``
    below is what makes that absence survivable: it does not reintroduce a zone, it removes the
    *need* for one, by pre-answering every local question as a UTC instant or a plain integer.

    ``week_starts_on`` stays here as a calendar *convention* — which day a week is considered to
    begin on — read by the caller that resolves ``LocalFrame.week_start``. It names no zone.
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
class LocalFrame:
    """This booking's local calendar, already resolved into UTC instants and plain integers.

    The engine has no timezone and the caller does. So the caller — the adapter, which is the only
    thing in the system that knows the venue's zone — answers every local question *before* the
    rule is called, and hands the answers over as absolute instants and numbers. A rule asking "is
    this a weekend booking", "how many bookings does this user already have in this week", "does
    this start before 9am" reads them from here and converts nothing.

    Every bound is resolved from **local midnight** and nothing else, so a day, a week and a month
    all begin when the *venue's* day begins rather than at UTC midnight. Each is a half-open
    ``[start, end)`` pair of two absolute instants: unlike ``AvailabilityHoursRule``'s deliberately
    invertible clock times, these describe no recurring window and have no wrap to represent, so an
    inverted pair is a caller bug and is rejected here.

    ``start_minutes`` and ``end_minutes`` are minutes from local midnight, derived from the instants
    rather than from any wall clock — which is what keeps them right on the two days a year a local
    day is 23 or 25 hours long. **``end_minutes`` may exceed 1440 and must never be capped**: that
    is a booking running past local midnight, and being able to say so is the point.

    There is no timezone field and no offset field. Not as a convenience, not "for debugging" — the
    absence is the invariant, and a rule holding a zone is a rule that can convert with it.
    """

    day_start: datetime
    day_end: datetime
    week_start: datetime
    week_end: datetime
    month_start: datetime
    month_end: datetime
    weekday: int
    start_minutes: int
    end_minutes: int

    def __post_init__(self) -> None:
        for label in ("day", "week", "month"):
            _validate_bounds(
                getattr(self, f"{label}_start"),
                getattr(self, f"{label}_end"),
                f"LocalFrame.{label}",
            )

        if isinstance(self.weekday, bool) or not isinstance(self.weekday, int):
            raise TypeError(f"LocalFrame.weekday must be an int, got {type(self.weekday).__name__}")
        if self.weekday not in range(7):
            raise ValueError(
                f"LocalFrame.weekday must be in range(7), 0 = Monday; got {self.weekday}"
            )

        start = _require_int(self.start_minutes, "LocalFrame.start_minutes")
        end = _require_int(self.end_minutes, "LocalFrame.end_minutes")
        if start < 0:
            raise ValueError(
                f"LocalFrame.start_minutes must be non-negative; got {start}. A booking before "
                "local midnight belongs to the previous local day, which is a different frame."
            )
        if end <= start:
            raise ValueError(
                f"LocalFrame.end_minutes must be strictly after start_minutes; got {end} <= {start}"
            )


@dataclass(frozen=True)
class RunContext:
    """The contiguous span of this user's bookings that the request sits in.

    Two consecutive bookings — 17:00-18:00 and 18:00-19:00 held by the same user — are, by default,
    one 2-hour session rather than two independent hours: the owner's stated intent
    (``ops/pending/bugs/max-duration-cannon.md``, "Resolution: the run") is that a member who books
    back-to-back is treated as if they had made one longer booking, unless a rule says otherwise. A
    rule that wants that reading judges ``context.run`` instead of the request; one that does not
    keeps reading ``request.duration`` exactly as before, unaffected by this field's existence.

    This is deliberately **not** built by rewriting ``request`` into the merged span and handing
    that to the engine instead. That obvious shortcut breaks ``NotInThePastRule`` (a merged start
    can sit before ``now`` for a booking that is genuinely extending one already in progress),
    ``LocalFrame`` (a merged start crossing local midnight resolves the frame for the wrong day),
    ``BookingHorizonRule`` (a merged start can only look closer than the real one, loosening the
    bound), and the counting rules (double-counting the same session unless history is rewritten
    too). Carrying the run as its own field means every one of those rules keeps seeing the request
    it was actually asked to judge, and only a rule that explicitly reads ``context.run`` opts into
    the merged view at all.

    ``start_at`` / ``end_at`` bound the whole merged run, not just the request; ``booking_count`` is
    how many rows compose it, the request itself included, so a request with no adjoining bookings
    still has ``booking_count == 1`` rather than ``0``. The adapter resolves this **transitively** —
    17-18 and 18-19 already held, a request for 19-20, merges into one 17-20 run — and **across
    every Resource in the Space**, since ``HistoryContext`` is already filtered to the user and
    never to one resource; a run is consequently not a real booking on any one court, which is the
    second reason it is never dressed up as a ``BookingRequest``.
    """

    start_at: datetime
    end_at: datetime
    booking_count: int

    def __post_init__(self) -> None:
        _validate_span(self.start_at, self.end_at, "RunContext")
        count = _require_int(self.booking_count, "RunContext.booking_count")
        if count < 1:
            raise ValueError(f"RunContext.booking_count must be at least 1; got {count}")

    @property
    def duration(self) -> timedelta:
        return self.end_at - self.start_at


@dataclass(frozen=True)
class HistoryContext:
    """The requesting user's relevant prior bookings.

    Filtered to the **user**, not to any one resource — a caller may draw these from several
    resources at once (an application counting a frequency cap across every Resource in a Space, for
    instance). Already capped and already filtered by the caller. Everything in here counts.
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
    ``local`` was the first time that promise was spent; ``run`` is the second, and it is spent the
    same way — as a **required field with no default**. The only default available for a run is "the
    request alone, count 1", which is exactly right for a request with no adjoining bookings and
    silently **permissive** for one the adapter simply forgot to resolve: a run-aware cap would then
    see a one-hour run where the user actually holds six. Silently permissive is the direction this
    codebase does not accept (``CLAUDE.md``, "Fail closed"). Every caller resolves a real run — even
    when that real run is just the request, which is the correct answer whenever history is empty,
    not a degraded stand-in for one — or it does not get a context.

    The history window invariant is enforced here rather than on ``HistoryContext`` because it is
    the only place both the history and ``now`` are visible. A rule must not be able to silently
    rely on history it will not be given in production.
    """

    user: UserContext
    calendar: CalendarContext
    local: LocalFrame
    run: RunContext
    history: HistoryContext = field(default_factory=HistoryContext)

    def __post_init__(self) -> None:
        if not isinstance(self.user, UserContext):
            raise TypeError(f"Context.user must be a UserContext, got {type(self.user).__name__}")
        if not isinstance(self.calendar, CalendarContext):
            raise TypeError(
                f"Context.calendar must be a CalendarContext, got {type(self.calendar).__name__}"
            )
        if not isinstance(self.local, LocalFrame):
            raise TypeError(f"Context.local must be a LocalFrame, got {type(self.local).__name__}")
        if not isinstance(self.run, RunContext):
            raise TypeError(f"Context.run must be a RunContext, got {type(self.run).__name__}")
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
