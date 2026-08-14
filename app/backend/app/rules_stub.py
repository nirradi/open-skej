"""The backend's adapter onto the rule engine.

The name is historical: this module began as a stub standing in for the engine
before it existed. It is now a thin **adapter** — it owns no rule logic. Every
verdict comes from ``rules.evaluate_request`` running a canon **assembled per
Space**, from that Space's own ``space_rules`` rows (task 6.6) read through
``rules.REGISTRY`` (task 6.4) — never from a hardcoded if-chain over scalar
columns, which is what this module used to be.

Callers build a ``BookingRequest``, a ``SpaceRuleConfig`` describing the Space
the booking is against, and (when the Space's canon includes a rule that reads
history) that user's prior bookings across the Space, then read
``RuleResult.allowed`` / ``.message``. Those three pydantic models are this
module's public surface, deliberately distinct from the engine's frozen
dataclasses of the same names — the API boundary validates untrusted input
from the wire, the engine boundary enforces UTC and the history window. This
module is where the two meet, and it stays **ORM-free**: it receives
``SpaceRuleConfig`` and history already extracted by the router, it does not
query for either.

Translations that happen here and nowhere else:

* **Timezone.** The engine rejects a non-zero UTC offset outright; this
  boundary converts every datetime to UTC before it reaches the engine. See
  ``_to_utc``. A ``space_rules`` row's own local values get a second,
  distinct timezone translation, per rule type, resolved fresh for every
  booking's own date rather than once at write time (``CLAUDE.md``,
  "Conversion happens at the boundary, per date, never once at write time"):
  a ``slot_alignment`` row's ``anchor`` resolves to the Space's own local
  midnight via ``_local_midnight_utc``; and the day/week/month counting rules'
  ``[window_start, window_end)`` resolve from the local day/week/month via
  ``_local_day_bounds`` / ``_local_week_bounds`` / ``_local_month_bounds``, the
  identical local day also serving ``max_duration_per_day`` (task 8.7). See
  ``_resolve_for_row``. ``availability_hours`` needs none of this any more —
  its params are already minutes from local midnight, read straight off
  ``context.local`` by the rule itself.
* **The local frame.** ``_build_local_frame`` answers every local question a
  rule could ask — the venue's day, week and month as UTC instants, the local
  weekday, and minutes from local midnight — and hands them over as
  ``Context.local``. This is the general form of the per-type resolution
  above: those four types get bespoke parameters because they were written
  before the frame existed, while a rule nobody hand-wrote can express a local
  day only through this. It is why the engine still carries no timezone
  anywhere (``.claude/rules/rule-engine.md``): this module remains the only
  thing in the system that knows one.
* **The run.** ``_resolve_run`` answers the one question ``LocalFrame`` does
  not: not "when is this", but "how much of this user's own history does this
  booking belong to". It resolves the contiguous, cross-Resource span of
  bookings ``request`` sits in — transitively, and closing a gap up to that
  date's own resolved minimum duration — and hands it over as ``Context.run``.
  A rule that wants to judge the whole run rather than the one request reads
  it there; every rule that does not is unaffected by its existence.
* **The counting rules' tolerance.** ``_resolve_for_row`` (task 8.6, extended by task 8.7) resolves
  the identical gap tolerance ``_resolve_run`` uses, for the three counting types — day, week,
  month — and hands it to ``MaxBookingsPerDayRule`` / ``MaxBookingsPerWeekRule`` /
  ``MaxBookingsPerMonthRule`` at construction. ``max_duration_per_day`` gets no tolerance at all —
  it sums raw entries rather than merging them into runs (see below). This module does **not**
  merge the request into ``Context.history`` itself — see ``rules/rules/frequency.py``'s module
  docstring for why that mechanism (the plan's stated preference) does not work: it conflicts with
  ``Context``'s own history-window invariant for any request beyond it. The three counting rules
  merge ``request`` with the raw ``context.history.bookings`` themselves, at evaluate time, which is
  why they alone need this resolved value. ``max_duration_per_day`` reads the identical raw
  ``context.history.bookings`` but sums the entries directly instead — see
  ``rules.frequency.MaxDurationPerDayRule``'s own docstring for why that rule is the one place in
  this stream runs are deliberately not used: a total is the same number however the day's
  bookings are grouped, except where a user holds two Resources at overlapping times, where a
  run's span is shorter than the two bookings added together and the total would silently
  under-count.
* **The allow-path message.** ``RuleResult(passed=True)`` carries no copy by
  design, but the API shows friendly text on success. ``ALLOWED_MESSAGE`` is
  supplied here.
* **Canon assembly.** ``_build_canon`` turns a ``SpaceRuleConfig`` into the
  ordered tuple of rules the controller runs — see its docstring for the
  filtering, resolution, and ordering it performs, and for the fail-closed
  path a row that cannot be built takes.

This module stays deliberately ORM-free, which is why it never resolves ``rule_type`` through
``app.rule_catalog`` directly: that module is nothing but ORM (it queries ``generated_rule_types``
on a miss), and importing it here would end the one property this module has always kept — that it
receives its inputs already extracted and never queries for any of them itself. Instead
``SpaceRuleConfig`` carries a ``lookup`` callable, defaulted to ``rules.REGISTRY.get`` so every
existing caller that builds one by hand keeps seeing only the hand-written types, and overridden by
``app.identity.service.space_rule_config`` to ``app.rule_catalog.catalog.lookup`` for the one caller
that wants generated types too. ``_build_canon`` and ``SpaceRuleConfig.reads_history`` both resolve
through it rather than through ``REGISTRY.get`` directly.
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator
from rules import (
    DEFAULT_CANON,
    REGISTRY,
    RULE_ERROR_MESSAGE,
    AvailabilityHoursRule,
    BaseRule,
    BookingHorizonRule,
    CalendarContext,
    HistoryContext,
    LocalFrame,
    MaxDurationRule,
    NotInThePastRule,
    RuleType,
    RunContext,
    UserContext,
    Weekday,
    evaluate_request,
    merge_adjoining_spans,
)
from rules import BookingRecord as EngineBookingRecord
from rules import BookingRequest as EngineBookingRequest
from rules import Context as EngineContext

logger = logging.getLogger(__name__)

ALLOWED_MESSAGE = "Looks good — this slot is available."

# The engine identifies a booking's user and resource by opaque string label —
# no canon rule branches on either — so these defaults are the engine boundary's
# own concern, deliberately independent of the data layer's integer foreign keys
# in ``app.db.constants``. A caller that has real ids passes them (stringified);
# these only fill the gap for a request built with neither.
_DEFAULT_USER_ID = "default-user"
_DEFAULT_RESOURCE_ID = "default-resource"

#: The week convention handed to ``CalendarContext``, which requires one.
#:
#: Stated rather than defaulted because the value a weekly cap counts against
#: must already be a decision somebody made, not whatever the constructor
#: happened to pick — and it is now read for real, by ``MaxBookingsPerWeekRule``,
#: whenever a Space's canon includes a ``max_bookings_per_week`` row.
WEEK_STARTS_ON = Weekday.MONDAY


def _canon_rule(rule_type: type):
    """Return the single instance of ``rule_type`` in ``rules.DEFAULT_CANON``.

    ``DEFAULT_CANON`` is not what the API runs — see the module docstring —
    but it remains the *reference* canon: the hand-written values the
    generation loop is measured against, and the ones the constants below
    mirror for callers (tests, the E2E copy-contract assertions) that want the
    values in force absent any per-Space override.
    """
    for rule in DEFAULT_CANON:
        if isinstance(rule, rule_type):
            return rule
    raise RuntimeError(f"DEFAULT_CANON no longer contains a {rule_type.__name__}")


#: Reference defaults, mirrored by ``app/e2e/tests/03-sad-path.spec.ts`` and the
#: backend suite. The canon actually run against a booking is assembled from
#: that booking's own Space's ``space_rules`` rows by ``_build_canon``, not
#: from these.
MAX_BOOKING_DURATION: timedelta = _canon_rule(MaxDurationRule).max_duration
AVAILABILITY_OPEN_MINUTES: int = _canon_rule(AvailabilityHoursRule).opens_at_minutes
AVAILABILITY_CLOSE_MINUTES: int = _canon_rule(AvailabilityHoursRule).closes_at_minutes
BOOKING_HORIZON_DAYS: int = _canon_rule(BookingHorizonRule).days


class BookingRequest(BaseModel):
    """A booking, at this boundary. Times are timezone-aware.

    Used both for the booking under evaluation and, shaped identically, for
    each entry in a caller's history — a past booking and a requested one share
    exactly these four fields. Any aware datetime is accepted, at any offset;
    the engine's own types accept UTC only, and ``_to_utc`` converts at the
    call, so a client that sends ``+02:00`` is served rather than rejected.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str = _DEFAULT_USER_ID
    resource_id: str = _DEFAULT_RESOURCE_ID
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def _check_interval(self) -> "BookingRequest":
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("start_at and end_at must be timezone-aware")
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        return self

    @property
    def duration(self) -> timedelta:
        return self.end_at - self.start_at


@dataclass(frozen=True)
class SpaceRuleRow:
    """One configured rule instance, as the adapter needs it to build a canon.

    Mirrors a ``space_rules`` row (``app.identity.models.SpaceRule``) closely
    enough to build from, while staying ORM-free like the rest of this module
    — the router extracts these fields from the rows it queried and this
    module never sees the ORM row itself.

    ``id`` breaks ties between two instances of the same ``rule_type`` when
    the assembled canon is sorted by declared priority
    (``.claude/rules/rule-engine.md``) — it should be the database row id, so
    two rows of the same type sort in the order they were created; a caller
    with no meaningful id (a unit test building a row from scratch) may pass
    any ``int``, since only the *relative* order across one Space's own rows
    is ever read.

    ``applies_to`` is ``None`` (always) or one of the two narrower shapes
    documented on ``SpaceRule`` — a weekday set or a date set, never both.
    """

    id: int
    rule_type: str
    params: dict
    applies_to: dict | None = None
    enabled: bool = True


#: A ``rule_type`` id resolved to its registered ``RuleType``, or ``None`` if this caller knows no
#: such type. ``app.rule_catalog.RuleTypeLookup`` is the same shape, defined again here rather than
#: imported — this module stays ORM-free and must not import that module (see the module
#: docstring), and the two definitions describe the same contract from either side of that seam.
RuleTypeLookup = Callable[[str], RuleType | None]


@dataclass(frozen=True)
class SpaceRuleConfig:
    """The rule-relevant slice of one Space's configuration.

    Built by the router from ``context.space_context.space``'s ``timezone``
    and a query for that Space's ``space_rules`` rows — never queried by this
    module itself (module docstring, "stays ORM-free"). ``timezone`` is the
    Space's IANA zone name, needed to resolve every row whose registered type
    has ``needs_local_resolution`` set.

    This used to be seven optional scalar fields, one per hardcoded rule
    column; it is now the zone plus ``rules``, a tuple of ``SpaceRuleRow`` —
    any number of instances of any registered type (task 6.6). A Space with
    no rows enforces nothing beyond ``NotInThePastRule``, exactly as a Space
    with every column null used to.

    ``lookup`` is how a row's ``rule_type`` resolves to a registered
    ``RuleType`` — passed in rather than hardcoded to ``REGISTRY.get``, since
    this module stays ORM-free and ``app.rule_catalog`` (the caller that also
    knows generated types) is nothing but ORM (module docstring). Defaulted
    to ``REGISTRY.get`` so every existing caller that builds a
    ``SpaceRuleConfig`` by hand — every test in this suite among them — keeps
    seeing only the nine hand-written types unless it opts in; the one
    caller that wants generated types too (``app.identity.service.space_rule_config``)
    passes ``app.rule_catalog.catalog.lookup`` explicitly. ``compare=False``
    and ``repr=False`` keep a bound method off this frozen dataclass's
    equality and repr, where it would otherwise make two configs built with
    the same rows compare unequal merely because they closed over different
    ``RuleCatalog`` instances.
    """

    timezone: str
    rules: tuple[SpaceRuleRow, ...] = field(default_factory=tuple)
    lookup: RuleTypeLookup = field(default=REGISTRY.get, compare=False, repr=False)

    @property
    def reads_history(self) -> bool:
        """Whether *any* enabled row's registered type reads booking history.

        The router uses this to decide whether to run the Space-wide history
        query at all: nothing configured that would read it means no query,
        preserving the "no wasted round trip" property this adapter has
        always documented for the case where nothing would read the result.

        Deliberately over-inclusive rather than exact. Whether a row's
        ``applies_to`` will actually match depends on the booking's own local
        date, which is not known yet at the point the router decides whether
        to run the history query — that decision happens once per request,
        before ``_build_canon`` ever resolves a date. Counting a row that
        turns out not to apply is a wasted query on the rare day it is scoped
        away; the reverse — skipping the query for a row that reads history
        and *would* apply — is the mistake that actually matters, since it
        would run a counting rule against no history at all. An unregistered
        ``rule_type`` reads as "does not read history" here; ``_build_canon``
        is what turns that row into a denial, and this property only ever
        decides whether to run a query, never whether to deny.
        """
        return any(
            row.enabled
            and (declared := self.lookup(row.rule_type)) is not None
            and declared.reads_history
            for row in self.rules
        )


class RuleResult(BaseModel):
    """The engine's verdict, as this API expresses it. ``message`` is user-facing.

    Distinct from the engine's ``RuleResult(passed, fail_reason)``: there,
    ``passed=True`` implies no copy at all. Here ``message`` is always populated,
    because the client renders it either way.
    """

    model_config = ConfigDict(frozen=True)

    allowed: bool
    message: str


class _UnbuildableRuleRowError(Exception):
    """A ``space_rules`` row could not be turned into a rule instance.

    Raised only by ``_build_canon`` and caught only by ``evaluate`` — it never
    crosses this module's boundary. Two causes, both configuration-integrity
    failures rather than a booking-time verdict: ``rule_type`` names nothing
    in ``REGISTRY`` (a type was renamed or retired out from under a live row),
    or ``params`` no longer satisfies that type's current schema (a required
    param went missing, or a stored value is the wrong shape) — see the
    decisions table in ``ops/plans/stream-6-plan.md``: "a rule that cannot be
    built is a denial, not a skip". Skipping would silently un-enforce a rule
    an admin believes is active; this is what keeps that from happening
    quietly.
    """


def _to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC for the engine.

    Load-bearing, not a formality. The engine rejects a non-zero offset outright
    rather than assuming one, so an unconverted ``+02:00`` value raises instead of
    being evaluated. Conversion is also the *correct* reading: availability hours
    are UTC clock times, so a booking must be judged on its UTC wall clock and not
    on whichever local one the client happened to serialise.

    A naive ``value`` is rejected rather than converted: ``datetime.astimezone``
    silently treats a naive input as the *system's* local time, which is exactly
    the silently-assumed timezone this codebase's UTC-everywhere invariant
    forbids (``CLAUDE.md``). ``BookingRequest`` already rejects a naive
    ``start_at``/``end_at`` at construction; this is what gives ``evaluate``'s
    own ``now`` argument the same guarantee, since it arrives as a bare
    ``datetime`` with no model validating it first.
    """
    if value.tzinfo is None:
        raise ValueError(f"value must be timezone-aware; got naive datetime {value!r}")
    return value.astimezone(timezone.utc)


def _local_date(instant: datetime, tz_name: str) -> date:
    """The calendar date ``instant`` (any aware datetime) falls on in ``tz_name``.

    Used to pick which date's rules apply — "conversion happens at the
    boundary, per date" (``CLAUDE.md``) means the date has to be the Space's
    own local one, not the UTC date the instant happens to also fall on,
    which can differ by a day near midnight in either direction. This is also
    the date every ``applies_to`` weekday/date match is judged against —
    computed in the Space's own zone, never UTC, which is the same bug class
    5.12 fixed in the counting rules, arriving in a second place here.
    """
    return instant.astimezone(ZoneInfo(tz_name)).date()


def _local_midnight_utc(day: date, tz_name: str) -> datetime:
    """The UTC instant at which ``day`` begins in ``tz_name``.

    The counting rules' boundaries and ``slot_alignment``'s anchor are both
    built from this and nothing else, so a week, a month, or a slot grid all
    start when the *venue's* day starts rather than at UTC midnight. On the
    handful of dates a zone's DST transition falls at or near local midnight,
    ``zoneinfo`` resolves the nonexistent or repeated instant by PEP 495 —
    accepted rather than special-cased: a second code path for a few hours a
    year would buy correctness nobody asked for and nobody could verify by
    reading.
    """
    return datetime.combine(day, time(0, 0), tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)


def _local_day_bounds(on_date: date, tz_name: str) -> tuple[datetime, datetime]:
    """The half-open ``[start, end)`` UTC bounds of the **local** day ``on_date``.

    The end is the local midnight of the *next date*, never ``start + 24h``. A local day is 23 or
    25 hours long across a DST transition, and a fixed timedelta would put the boundary an hour
    inside the neighbouring day — the identical error ``_local_week_bounds`` avoids one line down,
    arriving here in a third place. The plan that reintroduces it will look like a simplification.
    """
    return (
        _local_midnight_utc(on_date, tz_name),
        _local_midnight_utc(on_date + timedelta(days=1), tz_name),
    )


def _local_week_bounds(on_date: date, tz_name: str) -> tuple[datetime, datetime]:
    """The half-open ``[start, end)`` UTC bounds of the **local** week containing ``on_date``.

    ``Weekday`` is numbered to match :meth:`datetime.date.weekday`, so stepping
    back to the start of the week is a modular subtraction that holds for any
    choice of first day. The end is computed as *seven local days later*, not as
    ``start + 7 days``: across a DST transition the week is 167 or 169 hours
    long, and adding a fixed timedelta would put the boundary an hour into the
    neighbouring week — which is the whole class of bug this task exists to
    remove, reintroduced one line further down.
    """
    days_since_start = (on_date.weekday() - int(WEEK_STARTS_ON)) % 7
    first_day = on_date - timedelta(days=days_since_start)
    return (
        _local_midnight_utc(first_day, tz_name),
        _local_midnight_utc(first_day + timedelta(days=7), tz_name),
    )


def _local_month_bounds(on_date: date, tz_name: str) -> tuple[datetime, datetime]:
    """The half-open ``[start, end)`` UTC bounds of the **local** calendar month containing
    ``on_date``."""
    first_day = on_date.replace(day=1)
    next_month = (
        first_day.replace(year=first_day.year + 1, month=1)
        if first_day.month == 12
        else first_day.replace(month=first_day.month + 1)
    )
    return _local_midnight_utc(first_day, tz_name), _local_midnight_utc(next_month, tz_name)


def _build_local_frame(request: EngineBookingRequest, tz_name: str, on_date: date) -> LocalFrame:
    """Resolve every local question this booking could be asked, in the Space's own zone.

    This is the whole of the engine's knowledge of what "local" means, and it is resolved here
    because this adapter is the only thing in the system that holds a timezone. A rule reads the
    answers off ``context.local`` and converts nothing — ``CalendarContext`` carries no zone, and
    ``LocalFrame`` exists so that it never has to (``.claude/rules/rule-engine.md``).

    Every bound comes from ``_local_midnight_utc`` and nothing else, so the day, the week and the
    month all begin when the *venue's* day begins.

    ``start_minutes`` and ``end_minutes`` are derived from the instants rather than from a local
    wall clock, which is what keeps them right on a 23- or 25-hour day: on a spring-forward date the
    booking after the transition is genuinely 60 minutes closer to local midnight than its wall
    clock reads, and a rule bounding "nothing before 9am" is asking about elapsed local time.

    The start floors to whole minutes and the end **ceilings**. Rounding the end down would report a
    booking as finishing earlier than it does, which is the permissive direction, and it is what
    would let a sub-minute booking (nothing this product creates, but nothing this function may
    raise on either) collapse to ``end_minutes == start_minutes`` — a pair ``LocalFrame`` rejects.
    Every booking on whole minutes, which is all of them in practice, is unaffected.
    """
    day_start, day_end = _local_day_bounds(on_date, tz_name)
    week_start, week_end = _local_week_bounds(on_date, tz_name)
    month_start, month_end = _local_month_bounds(on_date, tz_name)
    return LocalFrame(
        day_start=day_start,
        day_end=day_end,
        week_start=week_start,
        week_end=week_end,
        month_start=month_start,
        month_end=month_end,
        weekday=on_date.weekday(),
        start_minutes=math.floor((request.start_at - day_start).total_seconds() / 60),
        end_minutes=math.ceil((request.end_at - day_start).total_seconds() / 60),
    )


def _engine_request(booking: BookingRequest) -> EngineBookingRequest:
    return EngineBookingRequest(
        user_id=booking.user_id,
        resource_id=booking.resource_id,
        start_at=_to_utc(booking.start_at),
        end_at=_to_utc(booking.end_at),
    )


def _engine_record(booking: BookingRequest) -> EngineBookingRecord:
    return EngineBookingRecord(
        user_id=booking.user_id,
        resource_id=booking.resource_id,
        start_at=_to_utc(booking.start_at),
        end_at=_to_utc(booking.end_at),
    )


def row_applies(applies_to: dict | None, on_date: date) -> bool:
    """Whether a row scoped by ``applies_to`` governs ``on_date``.

    ``on_date`` must already be the booking's own **local** date
    (``_local_date``, resolved in the Space's own zone) — computing the
    weekday from a UTC date here would reintroduce 5.12's bug class in a
    second place (``ops/plans/stream-6-plan.md``, "The weekday of a booking
    is computed in the Space's zone, never UTC").

    ``None`` means always, per ``SpaceRule``'s documented shape. Scoping is
    an adapter-level concern applied uniformly before the registry ever
    builds anything — a rule type declares no day/date handling of its own.

    Exported (task 6.9, no leading underscore) so ``resolve_day_schedule``
    can filter ``space_rules`` rows by the identical rule this module's own
    booking-evaluation path uses — a second implementation of "does this row
    apply to this date" could silently disagree with this one, which is
    exactly the bug class this codebase's docs keep warning about.
    """
    if applies_to is None:
        return True

    weekdays = applies_to.get("weekdays")
    if weekdays is not None:
        return on_date.weekday() in weekdays

    dates = applies_to.get("dates")
    if dates is not None:
        return on_date.isoformat() in dates

    # Neither facet key is present. Not a shape this adapter's own writers
    # ever produce, but not ambiguous either: no restriction named means no
    # restriction applied, matching `None`.
    return True


def _resolve_for_row(row: SpaceRuleRow, on_date: date, config: SpaceRuleConfig) -> dict:
    """Resolve whatever ``REGISTRY[row.rule_type]`` needs, from this row's own
    raw params, against the Space's zone and the booking's own local date.

    Only called for a row whose registered type declares
    ``needs_local_resolution`` — every local-to-UTC conversion happens here,
    at the adapter boundary, rather than inviting a rule type to convert for
    itself (``.claude/rules/rule-engine.md``). Each branch mirrors exactly
    what the pre-6.6 ``_build_canon`` did inline for the equivalent column.

    Takes the whole ``config`` rather than just its ``timezone`` since task 8.6: the three counting
    rows (day, week, month) also need a gap ``tolerance``, and resolving one needs
    ``resolve_day_schedule(config, on_date)`` — the same resolution ``evaluate``'s own
    ``_resolve_run`` call already reads a minimum duration from, for the identical reason
    (``rules.frequency``'s module docstring). ``max_duration_per_day`` (task 8.7) needs only the
    window and no tolerance at all — it sums raw history entries rather than merging them into
    runs, so ``resolve_day_schedule`` is not consulted for it.

    Raises ``KeyError``/``TypeError``/``ValueError`` for a row whose
    ``params`` do not have what this resolution needs (a missing
    ``slot_minutes``, a value of the wrong type) — ``_build_canon`` treats
    that identically to a failure inside ``RuleType.build`` itself, since both
    are "this row's stored params no longer satisfy its type's schema".
    """
    tz_name = config.timezone

    if row.rule_type == "slot_alignment":
        return {"anchor": _local_midnight_utc(on_date, tz_name)}

    if row.rule_type in ("max_bookings_per_day", "max_bookings_per_week", "max_bookings_per_month"):
        if row.rule_type == "max_bookings_per_day":
            window_start, window_end = _local_day_bounds(on_date, tz_name)
        elif row.rule_type == "max_bookings_per_week":
            window_start, window_end = _local_week_bounds(on_date, tz_name)
        else:
            window_start, window_end = _local_month_bounds(on_date, tz_name)
        tolerance_minutes = resolve_day_schedule(config, on_date).min_duration_minutes or 0
        return {
            "window_start": window_start,
            "window_end": window_end,
            "tolerance": timedelta(minutes=tolerance_minutes),
        }

    if row.rule_type == "max_duration_per_day":
        # The same local day bounds as `max_bookings_per_day` above, task 8.7's other new type —
        # but no `tolerance`: `MaxDurationPerDayRule` sums raw history entries rather than merging
        # them into runs, so there is no gap for a tolerance to close
        # (`rules/rules/frequency.py`'s module docstring, and the rule's own).
        window_start, window_end = _local_day_bounds(on_date, tz_name)
        return {"window_start": window_start, "window_end": window_end}

    # `REGISTRY[row.rule_type].needs_local_resolution` is true for exactly
    # these five types today (`rules/rules/registry.py`); a sixth arriving
    # without this adapter being taught how to resolve it is a genuine
    # adapter bug, not a bad configuration row, so it is left to raise loudly
    # rather than being folded into `_UnbuildableRuleRowError`.
    raise AssertionError(
        f"rule_type {row.rule_type!r} declares needs_local_resolution, but "
        "app.rules_stub._resolve_for_row has no case for it"
    )


def _build_canon(config: SpaceRuleConfig, on_date: date) -> tuple[BaseRule, ...]:
    """Assemble one Space's canon from its ``space_rules`` rows, in priority order.

    ``NotInThePastRule`` is prepended unconditionally — it is never optional,
    and it has no ``space_rules`` row: it takes no parameters and is always
    part of every Space's canon, exactly as before task 6.6.

    Every other row is, in order:

    1. **Dropped if disabled.** A disabled row is never assembled — pause is
       the entire mechanism (``ops/plans/stream-6-plan.md``).
    2. **Dropped if ``applies_to`` does not match ``on_date``** — see
       ``_row_applies``. Scoping happens before the registry ever builds
       anything, uniformly for every rule type.
    3. **Resolved through ``config.lookup``** — ``REGISTRY`` plus, for the
       one caller that wants them, the generated types
       ``app.rule_catalog.catalog`` has hoisted (see ``SpaceRuleConfig``'s
       own docstring). An id that resolves to nothing raises
       ``_UnbuildableRuleRowError``, caught by ``evaluate`` and turned into a
       denial — never a skip (see that exception's docstring). (``row_applies``
       above, not this step, is what ``resolve_day_schedule`` reuses; it needs
       no rule-type lookup at all — see that function.)
    4. **Resolved, if its type needs it** (``_resolve_for_row``), **and
       built** via ``RuleType.build``. A ``KeyError``/``TypeError``/
       ``ValueError`` from either step — a required param missing, a stored
       value that no longer satisfies the type's schema — is caught and
       re-raised as the same ``_UnbuildableRuleRowError``.

    The surviving rules are then **sorted by their type's declared
    ``priority``, then by row id** for two instances of the same type — never
    by row insertion order or any other order (``rule-engine.md``: the order
    arbitrates which denial message a user sees when several rules would
    refuse the same request, and that is a product decision made once per
    *type*, not something row order should get to redecide).

    ``on_date`` is the booking's own local date (``_local_date``), not the
    UTC date — the correct day's DST offset, and the correct local weekday
    for ``applies_to``, both depend on that.
    """
    canon: list[BaseRule] = [NotInThePastRule()]
    buildable: list[tuple[int, int, BaseRule]] = []

    for row in config.rules:
        if not row.enabled:
            continue
        if not row_applies(row.applies_to, on_date):
            continue

        rule_type = config.lookup(row.rule_type)
        if rule_type is None:
            raise _UnbuildableRuleRowError(
                f"space_rules row {row.id} names unregistered rule_type {row.rule_type!r}"
            )

        try:
            resolved = (
                _resolve_for_row(row, on_date, config) if rule_type.needs_local_resolution else None
            )
            rule = rule_type.build(row.params, resolved)
        except (KeyError, TypeError, ValueError) as exc:
            raise _UnbuildableRuleRowError(
                f"space_rules row {row.id} (rule_type={row.rule_type!r}, "
                f"params={row.params!r}) could not be built: {exc!r}"
            ) from exc

        buildable.append((rule_type.priority, row.id, rule))

    buildable.sort(key=lambda entry: (entry[0], entry[1]))
    canon.extend(rule for _, _, rule in buildable)
    return tuple(canon)


@dataclass(frozen=True)
class DaySchedule:
    """What a booking on one date would actually be judged against, resolved
    entirely in the Space's own local wall clock — never converted to UTC,
    unlike every other resolution this module performs.

    Built by ``resolve_day_schedule`` for ``GET /spaces/{public_id}/schedule``
    (task 6.9), the endpoint the calendar UI reads instead of re-deriving
    rule semantics itself: the engine stays the sole validator, so a second
    implementation of "what hours/slot size govern this date" in TypeScript
    is exactly the duplication ``DEFERRED.md`` item 13 warns against.

    ``slot_minutes`` / ``opens_at`` / ``closes_at`` / ``min_duration_minutes`` are ``None`` when no
    enabled, date-matching row of that type governs this date at all — the
    same "not enforced" convention ``SpaceRuleConfig`` and the frontend's
    ``CalendarConfig`` already use. ``coherence_issue`` is set only when a
    *real* (non-zero-width) resolved window's bounds do not land on the
    resolved slot grid; see ``resolve_day_schedule`` for why a zero-width
    window is not one of these cases.

    ``min_duration_minutes`` is resolved here rather than beside the one
    other reader that needs it: ``app.rules_stub._resolve_run`` sizes its own
    gap tolerance from the identical resolution, and a second implementation
    of "what minimum duration governs this date" here would be exactly the
    drift this codebase's docs keep warning about. It is reported on the wire
    by ``DayScheduleRead``, alongside its own coherence case against the
    operating window (below) — the frontend's own ``CalendarConfig`` does not
    read it yet, since rendering a click unit that honours it is separate,
    larger UI work.
    """

    slot_minutes: int | None
    opens_at: time | None
    closes_at: time | None
    coherence_issue: str | None
    min_duration_minutes: int | None


def _minutes_to_wire_time(minutes: int | None) -> time | None:
    """Render a minutes-from-local-midnight integer as the wire's ``time`` shape.

    ``None`` stays ``None`` — "not configured", not midnight. Otherwise ``minutes % 1440`` folds a
    value at or past 24 hours back onto an ordinary wall clock: the *only* place in this function
    that reduces a minute count to a bare ``time``, and deliberately the very last step (see
    ``resolve_day_schedule``'s docstring for why every computation above this stays in minutes).
    """
    if minutes is None:
        return None
    minutes = minutes % 1440
    return time(minutes // 60, minutes % 60)


def resolve_day_schedule(config: SpaceRuleConfig, on_date: date) -> DaySchedule:
    """What a booking on ``on_date`` would actually be judged against, in the
    Space's own local wall clock.

    Mirrors ``_build_canon``'s own filtering (``row.enabled`` and
    ``row_applies``) but stays local rather than resolving to UTC — this
    endpoint's whole point is to report the Space's own wall-clock hours and
    slot size, which is what the calendar grid draws itself from — there is no
    instant to judge here, only a calendar date to describe. Unlike
    ``_build_canon`` this never
    touches ``REGISTRY`` or ``RuleType.build``: an ``availability_hours``
    row's ``opens_at_minutes``/``closes_at_minutes`` and a ``slot_alignment``
    row's local ``slot_minutes`` are read directly off ``params``, with no
    anchor to resolve — the anchor is always the date's own local midnight,
    which is irrelevant to what this endpoint reports.

    **Every matching row of a type must hold simultaneously** — "the engine
    stays a flat AND of deny predicates" (``ops/plans/stream-6-plan.md``,
    Decisions) — so two or more matching rows of one type are *combined*,
    never picked from:

    * ``availability_hours`` — the **intersection** of every matching row's
      own window: ``effective_open = max(opens_at_minutes)``,
      ``effective_close = min(closes_at_minutes)``, computed **entirely in
      integer minutes** and converted to the wire's ``time`` shape only in
      the final step (``_minutes_to_wire_time``). A single row can never
      itself be inverted (``AvailabilityHoursRule.__init__`` enforces
      ``opens_at_minutes < closes_at_minutes``), but the intersection of two
      or more legitimately can be — "9-12" and "14-18" together permit
      nothing. That is a real flat-AND outcome ("closed all day on this
      date"), not a coherence error, so it is normalised to a **zero-width**
      window (``effective_close = effective_open``) rather than reported as
      broken. Doing this comparison in minutes rather than converting each
      row to a bare ``time`` first is load-bearing, not a style choice: a
      row's own window may now legitimately cross local midnight
      (``closes_at_minutes > 1440``), and reducing such a row to a ``time``
      before comparing would make it look "inverted" on its own — the exact
      state this zero-width normalisation exists to flag for a *genuine*
      empty intersection — and wrongly report a real 18:00–02:00 window as
      closed all day even with only one matching row.
    * ``slot_alignment`` — the **LCM** of every matching row's own
      ``slot_minutes``: a date must land on a multiple of *every* matching
      row's own grid simultaneously, and being divisible by the LCM is
      exactly that (not the minimum, which does not make every row's grid a
      subset of it). Every individually stored ``slot_minutes`` already
      divides 1440 (``SlotAlignmentRule.__init__``), so the LCM of any two
      such divisors also divides 1440 — this can never itself produce an
      incoherent day length.
    * ``min_duration`` — the **maximum** of every matching row's own
      ``min_duration_minutes``: satisfying two floors at once means clearing
      the higher one, the mirror image of the availability intersection
      above (there, satisfying two windows at once means the narrower
      overlap; here, it means the larger floor). Unlike the other two types
      this is read directly off ``params`` with no comparison against the
      slot grid — it has its own coherence case against the operating window
      instead, below.

    No matching row of a type at all resolves to ``None`` for it, matching
    ``SpaceRuleConfig``'s and the frontend's ``CalendarConfig``'s "not
    configured" shape.

    ``coherence_issue`` fires in either of two, mutually exclusive cases, the
    second checked only once the first has cleared:

    1. A **real** (non-zero-width) resolved window *and* a resolved slot size
       whose boundaries do not land on it — mirroring ``config.ts``'s own
       ``coherenceIssue`` wording. A zero-width "closed all day" window is
       never flagged: there is no grid to misalign with nothing bookable in
       it.
    2. A real resolved window *and* a resolved minimum duration longer than
       the window itself — nothing on that date could ever clear the
       minimum, so the window is bookable in name only. This is checked only
       when case 1 did not already fire, and only against the window's own
       length, never against the slot grid: a minimum duration that is
       merely not a multiple of the slot size is not an error here at all —
       the calendar's click unit rounds up to cover it (task 8.3) — so this
       case fires on *length*, never on divisibility.

    **The wire shape is unchanged and a same-day window renders exactly as before** —
    ``DaySchedule.opens_at``/``closes_at`` are still ``time | None``, and the only kind of window
    this product has ever configured (``closes_at_minutes <= 1440``) reduces through
    ``_minutes_to_wire_time`` to the identical wall-clock pair the old ``time``-based
    implementation produced. A window that crosses local midnight — newly representable in the
    engine by this task, not by this endpoint — is the one honest limitation left: it reduces to a
    ``closes_at`` that reads *earlier* than ``opens_at`` as a bare wall-clock value, which the
    calendar grid does not yet render as a wrapping window
    (``ops/done/stream-7/passed-midnight.md``'s own "Correction" section). This endpoint still must
    not crash or misreport coherence for such a Space, and it does not; teaching the grid to draw a
    wrapping day is separate, larger UI work this task is not asked to do.
    """
    matching = [row for row in config.rules if row.enabled and row_applies(row.applies_to, on_date)]

    hours_rows = [row for row in matching if row.rule_type == "availability_hours"]
    if hours_rows:
        opens_at_minutes = max(int(row.params["opens_at_minutes"]) for row in hours_rows)
        closes_at_minutes = min(int(row.params["closes_at_minutes"]) for row in hours_rows)
        if closes_at_minutes <= opens_at_minutes:
            # The intersection of two or more matching windows can
            # legitimately come out empty (see docstring above) — normalise
            # to a zero-width window rather than report it broken.
            closes_at_minutes = opens_at_minutes
    else:
        opens_at_minutes = None
        closes_at_minutes = None

    slot_rows = [row for row in matching if row.rule_type == "slot_alignment"]
    slot_minutes = (
        math.lcm(*(int(row.params["slot_minutes"]) for row in slot_rows)) if slot_rows else None
    )

    duration_rows = [row for row in matching if row.rule_type == "min_duration"]
    min_duration_minutes = (
        max(int(row.params["min_duration_minutes"]) for row in duration_rows)
        if duration_rows
        else None
    )

    coherence_issue: str | None = None
    if (
        opens_at_minutes is not None
        and closes_at_minutes is not None
        and closes_at_minutes != opens_at_minutes
        and slot_minutes is not None
    ):
        if opens_at_minutes % slot_minutes != 0:
            coherence_issue = f"Opening time must land on a {slot_minutes}-minute slot boundary."
        elif closes_at_minutes % slot_minutes != 0:
            coherence_issue = f"Closing time must land on a {slot_minutes}-minute slot boundary."

    if (
        coherence_issue is None
        and opens_at_minutes is not None
        and closes_at_minutes is not None
        and closes_at_minutes != opens_at_minutes
        and min_duration_minutes is not None
        and min_duration_minutes > closes_at_minutes - opens_at_minutes
    ):
        coherence_issue = "Minimum duration must not exceed the length of the operating window."

    return DaySchedule(
        slot_minutes=slot_minutes,
        opens_at=_minutes_to_wire_time(opens_at_minutes),
        closes_at=_minutes_to_wire_time(closes_at_minutes),
        coherence_issue=coherence_issue,
        min_duration_minutes=min_duration_minutes,
    )


def _resolve_run(
    request: EngineBookingRequest,
    history: tuple[EngineBookingRecord, ...],
    tolerance: timedelta,
) -> RunContext:
    """Resolve the contiguous run ``request`` belongs to, per ``max-duration-cannon.md``'s
    decisions.

    ``history`` is already the Space-wide, user-filtered set ``evaluate`` builds
    ``HistoryContext`` from — cross-resource falls out of that input rather than needing to be
    built here, since a run is not a real booking on any one court in the first place
    (``RunContext``'s own docstring).

    ``rules.spans.merge_adjoining_spans`` (task 8.4) does the actual sweep — shared with
    ``MaxBookingsPerWeekRule`` / ``MaxBookingsPerMonthRule`` (task 8.6,
    ``rules/rules/frequency.py``), which call it themselves rather than reading a pre-merged
    ``Context.history``; see that module's docstring for why the merge happens in the rule and not
    here.

    The request's own span is folded into the same sweep as every history entry rather than
    merged against the result afterwards — a two-pass version (merge history, then check whether
    the request touches one end of the result) would silently miss a request that lands between
    two *history* entries close enough to bridge them, which is exactly the case a run-aware rule
    most needs to see. Folding it in up front means the closure the request changes is the one
    this function returns.

    When ``history`` is empty — every Space whose canon reads no history at all — the only span
    in the sweep is the request's own, so the run returned is the request alone with
    ``booking_count=1``. That is the correct answer, not a degraded one: nothing else exists for
    this booking to have joined.
    """
    spans = [(request.start_at, request.end_at)] + [
        (booking.start_at, booking.end_at) for booking in history
    ]
    for run in merge_adjoining_spans(spans, tolerance):
        if run.start_at <= request.start_at and request.end_at <= run.end_at:
            return RunContext(
                start_at=run.start_at, end_at=run.end_at, booking_count=run.booking_count
            )

    # Unreachable: `request`'s own span is always one of `spans`, so it is always inside exactly
    # one of `merge_adjoining_spans`' returned runs. An `AssertionError` here would mean the sweep
    # itself is broken, not that this booking is unusual.
    raise AssertionError("the request's own span was not found in its own merge closure")


def evaluate(
    booking: BookingRequest,
    config: SpaceRuleConfig,
    history: tuple[BookingRequest, ...] = (),
    *,
    now: datetime | None = None,
) -> RuleResult:
    """Run ``booking``'s Space's own canon against it and return the verdict.

    ``history`` is this user's prior confirmed bookings **across every
    Resource in the Space** — the router loads it, capped to
    ``rules.history_window``, only when ``config.reads_history`` is true, and
    passes it empty otherwise. Every entry counts; nothing here filters it
    further (``.claude/rules/rule-engine.md``, "everything in it counts").

    ``now`` defaults to the live UTC clock; tests pin it explicitly instead of
    racing the wall clock.

    ``context.run`` is resolved from ``history`` and ``booking`` together by
    ``_resolve_run``, with a gap tolerance equal to this date's own resolved
    minimum duration (``resolve_day_schedule``, zero when no ``min_duration``
    row governs the date) — see that function's docstring for why a
    tolerance is needed at all. This runs whether or not the canon reads
    history: an empty ``history`` still resolves a run, the request alone.

    ``context.history`` carries ``history`` as-is, raw rows, unmerged (unchanged since before task
    8.6). ``MaxBookingsPerWeekRule`` / ``MaxBookingsPerMonthRule`` merge it with the request
    themselves, at evaluate time, rather than reading a pre-merged field here — see
    ``rules/rules/frequency.py``'s module docstring for why: folding the request into
    ``Context.history`` here would let a request beyond ``rules.history_window`` (any booking past
    the current month or a week out — the ordinary case for a `BookingHorizonRule` weeks or months
    wide) violate ``Context``'s own history-window invariant on construction, denying an otherwise
    unremarkable future booking outright.

    A ``space_rules`` row that cannot be built into a rule — an unregistered
    ``rule_type``, or ``params`` that no longer satisfy that type's schema —
    denies the booking with ``RULE_ERROR_MESSAGE`` rather than skipping the
    row or raising past this function: fail closed on a bad row, logged
    loudly so whoever can fix the Space's configuration finds out. This is
    checked *before* ``evaluate_request`` ever runs, since it is a
    configuration-integrity failure, not a rule's verdict on this booking.

    ``ContextMismatchError`` is not caught. The request and the context are
    both built here from ``booking`` and ``history``, so a mismatch cannot be
    a client error — it would be a bug in this adapter, and the engine raises
    precisely so that it reaches the error tracker instead of being served as
    a polite refusal. Every other failure inside a rule is already contained
    by the controller and arrives as an ordinary denial.
    """
    utc_now = _to_utc(now) if now is not None else datetime.now(timezone.utc)
    engine_request = _engine_request(booking)
    on_date = _local_date(engine_request.start_at, config.timezone)

    try:
        canon = _build_canon(config, on_date)
    except _UnbuildableRuleRowError:
        logger.error(
            "A space_rules row could not be built while evaluating a booking request for "
            "user %s on resource %s; denying the request.",
            booking.user_id,
            booking.resource_id,
            exc_info=True,
        )
        return RuleResult(allowed=False, message=RULE_ERROR_MESSAGE)

    history_records = tuple(_engine_record(entry) for entry in history)
    tolerance_minutes = resolve_day_schedule(config, on_date).min_duration_minutes or 0
    run = _resolve_run(engine_request, history_records, timedelta(minutes=tolerance_minutes))

    engine_context = EngineContext(
        user=UserContext(user_id=booking.user_id),
        calendar=CalendarContext(week_starts_on=WEEK_STARTS_ON, now=utc_now),
        local=_build_local_frame(engine_request, config.timezone, on_date),
        run=run,
        history=HistoryContext(bookings=history_records),
    )

    result = evaluate_request(engine_request, engine_context, canon)

    if result.passed:
        # The engine drops the message on the allow path (``passed=True`` implies
        # ``fail_reason is None``); the success banner is this layer's copy to write.
        return RuleResult(allowed=True, message=ALLOWED_MESSAGE)
    return RuleResult(allowed=False, message=result.fail_reason)
