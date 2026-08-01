"""The backend's adapter onto the rule engine.

The name is historical: this module began as a stub standing in for the engine
before it existed. It is now a thin **adapter** — it owns no rule logic. Every
verdict comes from ``rules.evaluate_request`` running a canon **assembled per
Space**, from that Space's own configuration (``.claude/rules/identity-and-
access.md``, "A Space is the unit of configuration; a Resource is capacity").

Callers build a ``BookingRequest``, a ``SpaceRuleConfig`` describing the Space
the booking is against, and (when the Space counts bookings) that user's prior
bookings across the Space, then read ``RuleResult.allowed`` / ``.message``.
Those three pydantic models are this module's public surface, deliberately
distinct from the engine's frozen dataclasses of the same names — the API
boundary validates untrusted input from the wire, the engine boundary
enforces UTC and the history window. This module is where the two meet, and
it stays **ORM-free**: it receives ``SpaceRuleConfig`` and history already
extracted by the router, it does not query for either.

Translations that happen here and nowhere else:

* **Timezone.** The engine rejects a non-zero UTC offset outright; this
  boundary converts every datetime to UTC before it reaches the engine. See
  ``_to_utc``. Availability hours are a second, distinct timezone translation:
  a Space's ``opens_at``/``closes_at`` are *local* wall-clock hours, resolved
  to a UTC window for the booking's own date via ``app.operating_hours``
  before ``AvailabilityHoursRule`` — which only ever speaks UTC — is built.
  See ``_local_date`` and ``_build_canon``. A Space's ``slot_minutes`` gets a
  third: ``SlotAlignmentRule`` needs a UTC ``anchor`` instant, resolved as the
  Space's own local midnight for the booking's date via ``_local_midnight_utc``
  — never ``opens_at``, so its grid stays independent of the hours rule's own
  parameter.
* **The allow-path message.** ``RuleResult(passed=True)`` carries no copy by
  design, but the API shows friendly text on success. ``ALLOWED_MESSAGE`` is
  supplied here.
* **Canon assembly.** ``_build_canon`` turns a ``SpaceRuleConfig`` into the
  ordered tuple of rules the controller runs — see its docstring for the
  order and why a null column omits a rule rather than passing a vacuous one.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator
from rules import (
    DEFAULT_CANON,
    AvailabilityHoursRule,
    BaseRule,
    BookingHorizonRule,
    CalendarContext,
    HistoryContext,
    MaxBookingsPerMonthRule,
    MaxBookingsPerWeekRule,
    MaxDurationRule,
    NotInThePastRule,
    SlotAlignmentRule,
    UserContext,
    Weekday,
    evaluate_request,
)
from rules import BookingRecord as EngineBookingRecord
from rules import BookingRequest as EngineBookingRequest
from rules import Context as EngineContext

from app.operating_hours import resolve_operating_hours

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
#: whenever a Space's ``max_bookings_per_week`` is set.
WEEK_STARTS_ON = Weekday.MONDAY


def _canon_rule(rule_type: type):
    """Return the single instance of ``rule_type`` in ``rules.DEFAULT_CANON``.

    ``DEFAULT_CANON`` is no longer what the API runs — see the module
    docstring — but it remains the *reference* canon: the hand-written values
    the generation loop is measured against, and the ones the constants below
    mirror for callers (tests, the E2E copy-contract assertions) that want the
    values in force absent any per-Space override.
    """
    for rule in DEFAULT_CANON:
        if isinstance(rule, rule_type):
            return rule
    raise RuntimeError(f"DEFAULT_CANON no longer contains a {rule_type.__name__}")


#: Reference defaults, mirrored by ``app/e2e/tests/03-sad-path.spec.ts`` and the
#: backend suite. The canon actually run against a booking is assembled from
#: that booking's own Space's configuration by ``_build_canon``, not from these.
MAX_BOOKING_DURATION: timedelta = _canon_rule(MaxDurationRule).max_duration
AVAILABILITY_OPEN: time = _canon_rule(AvailabilityHoursRule).opens_at
AVAILABILITY_CLOSE: time = _canon_rule(AvailabilityHoursRule).closes_at
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
class SpaceRuleConfig:
    """The rule-relevant slice of one Space's configuration.

    Built by the router from ``context.space_context.space``, never queried by
    this module — see the module docstring on why ``rules_stub`` stays
    ORM-free. ``timezone`` is the Space's IANA zone name, needed to resolve
    ``opens_at``/``closes_at`` (local wall-clock) to a UTC window per date.

    Every field but ``timezone`` is nullable, mirroring the ``spaces`` columns:
    null means the corresponding rule is not enforced for this Space
    (``.claude/rules/identity-and-access.md``). ``NotInThePastRule`` has no
    field here because it is never optional — you can never book the past,
    Space configuration or not.
    """

    timezone: str
    opens_at: time | None = None
    closes_at: time | None = None
    max_duration_minutes: int | None = None
    booking_horizon_days: int | None = None
    slot_minutes: int | None = None
    max_bookings_per_week: int | None = None
    max_bookings_per_month: int | None = None

    @property
    def counts_history(self) -> bool:
        """Whether this Space's canon includes a rule that reads booking history.

        The router uses this to decide whether to run the Space-wide history
        query at all: no counting rule configured means no query, preserving
        the "no wasted round trip" property this adapter has always documented
        for the case where nothing would read the result.
        """
        return self.max_bookings_per_week is not None or self.max_bookings_per_month is not None


class RuleResult(BaseModel):
    """The engine's verdict, as this API expresses it. ``message`` is user-facing.

    Distinct from the engine's ``RuleResult(passed, fail_reason)``: there,
    ``passed=True`` implies no copy at all. Here ``message`` is always populated,
    because the client renders it either way.
    """

    model_config = ConfigDict(frozen=True)

    allowed: bool
    message: str


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

    Used to pick which date's operating hours to resolve — "conversion happens
    at the boundary, per date" (``CLAUDE.md``) means the date has to be the
    Space's own local one, not the UTC date the instant happens to also fall
    on, which can differ by a day near midnight in either direction.
    """
    return instant.astimezone(ZoneInfo(tz_name)).date()


def _local_midnight_utc(day: date, tz_name: str) -> datetime:
    """The UTC instant at which ``day`` begins in ``tz_name``.

    The counting rules' boundaries are built from this and nothing else, so a
    week or a month starts when the *venue's* day starts rather than at UTC
    midnight. On the handful of dates a zone's DST transition falls at or near
    local midnight, ``zoneinfo`` resolves the nonexistent or repeated instant by
    PEP 495 — the same treatment ``app.operating_hours`` already gives operating
    hours, and for the same reason: a second code path for a few hours a year
    would buy correctness nobody asked for and nobody could verify by reading.
    """
    return datetime.combine(day, time(0, 0), tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)


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


def _build_canon(config: SpaceRuleConfig, on_date: date) -> tuple[BaseRule, ...]:
    """Assemble one Space's canon, in the order that arbitrates denial copy.

    ``evaluate_request`` is fail-fast, so the first rule to deny decides the
    single message a user sees when a request breaks several of the Space's
    rules at once. The order mirrors ``rules.canon.default_canon``'s own
    rationale, with ``SlotAlignmentRule`` inserted beside duration and hours
    and the two counting rules appended after it:

    1. ``NotInThePastRule`` — always present; you can never book the past.
    2. ``BookingHorizonRule`` — a date rule, so it runs before any remedy the
       user could apply *within* an otherwise-bookable date.
    3. ``MaxDurationRule`` — a remedy (shorten it) for a date that is fine.
    4. ``SlotAlignmentRule`` — another within-date remedy (line the booking up
       with the grid), so it sits beside duration and hours rather than with
       the date rules above it or the counting rules below.
    5. ``AvailabilityHoursRule`` — the other within-date remedy (pick another
       time), so it follows duration and slot alignment for the same reason
       duration follows the date rules.
    6. ``MaxBookingsPerWeekRule`` / 7. ``MaxBookingsPerMonthRule`` — appended
       last: unlike the five above, a denial here is not something changing
       *this* request can fix at all — no shorter, earlier, or later booking
       clears a frequency cap — so it is the least actionable reason to lead
       with, and every rule that names a fixable problem gets first refusal.

    Each rule is included only when its Space column is set; null means "not
    enforced" (``.claude/rules/identity-and-access.md``). Availability needs
    *both* ``opens_at`` and ``closes_at`` before it is built at all.

    ``on_date`` is passed to ``resolve_operating_hours`` unchanged; it is the
    booking's start date in the Space's own local zone (``_local_date``), not
    the UTC date, so the correct day's DST offset resolves the window.
    ``SlotAlignmentRule``'s ``anchor`` is resolved from the same ``on_date``,
    via ``_local_midnight_utc`` — the Space's own local midnight, never
    ``opens_at``: anchoring the grid on the hours rule's parameter would
    couple two independent rule instances and break the moment the two are
    scoped to different days via a future ``applies_to``.
    """
    canon: list[BaseRule] = [NotInThePastRule()]

    if config.booking_horizon_days is not None:
        canon.append(BookingHorizonRule(days=config.booking_horizon_days))

    if config.max_duration_minutes is not None:
        canon.append(MaxDurationRule(max_duration=timedelta(minutes=config.max_duration_minutes)))

    if config.slot_minutes is not None:
        anchor = _local_midnight_utc(on_date, config.timezone)
        canon.append(SlotAlignmentRule(slot_minutes=config.slot_minutes, anchor=anchor))

    if config.opens_at is not None and config.closes_at is not None:
        utc_open, utc_close = resolve_operating_hours(
            config.opens_at, config.closes_at, config.timezone, on_date
        )
        canon.append(AvailabilityHoursRule(opens_at=utc_open, closes_at=utc_close))

    if config.max_bookings_per_week is not None:
        week_start, week_end = _local_week_bounds(on_date, config.timezone)
        canon.append(
            MaxBookingsPerWeekRule(
                max_bookings=config.max_bookings_per_week,
                window_start=week_start,
                window_end=week_end,
            )
        )

    if config.max_bookings_per_month is not None:
        month_start, month_end = _local_month_bounds(on_date, config.timezone)
        canon.append(
            MaxBookingsPerMonthRule(
                max_bookings=config.max_bookings_per_month,
                window_start=month_start,
                window_end=month_end,
            )
        )

    return tuple(canon)


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
    ``rules.history_window``, only when ``config.counts_history`` is true, and
    passes it empty otherwise. Every entry counts; nothing here filters it
    further (``.claude/rules/rule-engine.md``, "everything in it counts").

    ``now`` defaults to the live UTC clock; tests pin it explicitly instead of
    racing the wall clock.

    ``ContextMismatchError`` is not caught. The request and the context are
    both built here from ``booking`` and ``history``, so a mismatch cannot be
    a client error — it would be a bug in this adapter, and the engine raises
    precisely so that it reaches the error tracker instead of being served as
    a polite refusal. Every other failure inside a rule is already contained
    by the controller and arrives as an ordinary denial.
    """
    utc_now = _to_utc(now) if now is not None else datetime.now(timezone.utc)
    engine_request = _engine_request(booking)

    canon = _build_canon(config, _local_date(engine_request.start_at, config.timezone))

    engine_context = EngineContext(
        user=UserContext(user_id=booking.user_id),
        calendar=CalendarContext(week_starts_on=WEEK_STARTS_ON, now=utc_now),
        history=HistoryContext(bookings=tuple(_engine_record(entry) for entry in history)),
    )

    result = evaluate_request(engine_request, engine_context, canon)

    if result.passed:
        # The engine drops the message on the allow path (``passed=True`` implies
        # ``fail_reason is None``); the success banner is this layer's copy to write.
        return RuleResult(allowed=True, message=ALLOWED_MESSAGE)
    return RuleResult(allowed=False, message=result.fail_reason)
