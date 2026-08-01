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
  an ``availability_hours`` row's local ``opens_at``/``closes_at`` resolve to
  a UTC window via ``app.operating_hours``; a ``slot_alignment`` row's
  ``anchor`` resolves to the Space's own local midnight via
  ``_local_midnight_utc``; and both counting rules' ``[window_start,
  window_end)`` resolve from the local week/month via ``_local_week_bounds``
  / ``_local_month_bounds``. See ``_resolve_for_row``.
* **The allow-path message.** ``RuleResult(passed=True)`` carries no copy by
  design, but the API shows friendly text on success. ``ALLOWED_MESSAGE`` is
  supplied here.
* **Canon assembly.** ``_build_canon`` turns a ``SpaceRuleConfig`` into the
  ordered tuple of rules the controller runs — see its docstring for the
  filtering, resolution, and ordering it performs, and for the fail-closed
  path a row that cannot be built takes.
"""

import logging
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
    MaxDurationRule,
    NotInThePastRule,
    UserContext,
    Weekday,
    evaluate_request,
)
from rules import BookingRecord as EngineBookingRecord
from rules import BookingRequest as EngineBookingRequest
from rules import Context as EngineContext

from app.operating_hours import resolve_operating_hours

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
    """

    timezone: str
    rules: tuple[SpaceRuleRow, ...] = field(default_factory=tuple)

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
            and (declared := REGISTRY.get(row.rule_type)) is not None
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
    ``zoneinfo`` resolves the nonexistent or repeated instant by PEP 495 — the
    same treatment ``app.operating_hours`` already gives operating hours, and
    for the same reason: a second code path for a few hours a year would buy
    correctness nobody asked for and nobody could verify by reading.
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


def _row_applies(applies_to: dict | None, on_date: date) -> bool:
    """Whether a row scoped by ``applies_to`` governs ``on_date``.

    ``on_date`` must already be the booking's own **local** date
    (``_local_date``, resolved in the Space's own zone) — computing the
    weekday from a UTC date here would reintroduce 5.12's bug class in a
    second place (``ops/plans/stream-6-plan.md``, "The weekday of a booking
    is computed in the Space's zone, never UTC").

    ``None`` means always, per ``SpaceRule``'s documented shape. Scoping is
    an adapter-level concern applied uniformly before the registry ever
    builds anything — a rule type declares no day/date handling of its own.
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


def _resolve_for_row(row: SpaceRuleRow, on_date: date, tz_name: str) -> dict:
    """Resolve whatever ``REGISTRY[row.rule_type]`` needs, from this row's own
    raw params, against the Space's zone and the booking's own local date.

    Only called for a row whose registered type declares
    ``needs_local_resolution`` — every local-to-UTC conversion happens here,
    at the adapter boundary, rather than inviting a rule type to convert for
    itself (``.claude/rules/rule-engine.md``). Each branch mirrors exactly
    what the pre-6.6 ``_build_canon`` did inline for the equivalent column.

    Raises ``KeyError``/``TypeError``/``ValueError`` for a row whose
    ``params`` do not have what this resolution needs (a missing
    ``opens_at``, an unparseable time string) — ``_build_canon`` treats that
    identically to a failure inside ``RuleType.build`` itself, since both are
    "this row's stored params no longer satisfy its type's schema".
    """
    if row.rule_type == "availability_hours":
        opens_at = time.fromisoformat(row.params["opens_at"])
        closes_at = time.fromisoformat(row.params["closes_at"])
        utc_open, utc_close = resolve_operating_hours(opens_at, closes_at, tz_name, on_date)
        return {"opens_at": utc_open, "closes_at": utc_close}

    if row.rule_type == "slot_alignment":
        return {"anchor": _local_midnight_utc(on_date, tz_name)}

    if row.rule_type == "max_bookings_per_week":
        window_start, window_end = _local_week_bounds(on_date, tz_name)
        return {"window_start": window_start, "window_end": window_end}

    if row.rule_type == "max_bookings_per_month":
        window_start, window_end = _local_month_bounds(on_date, tz_name)
        return {"window_start": window_start, "window_end": window_end}

    # `REGISTRY[row.rule_type].needs_local_resolution` is true for exactly
    # these four types today (`rules/rules/registry.py`); a fifth arriving
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
    3. **Looked up in ``REGISTRY`` by ``rule_type``.** An id not registered
       raises ``_UnbuildableRuleRowError``, caught by ``evaluate`` and turned
       into a denial — never a skip (see that exception's docstring).
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
        if not _row_applies(row.applies_to, on_date):
            continue

        rule_type = REGISTRY.get(row.rule_type)
        if rule_type is None:
            raise _UnbuildableRuleRowError(
                f"space_rules row {row.id} names unregistered rule_type {row.rule_type!r}"
            )

        try:
            resolved = (
                _resolve_for_row(row, on_date, config.timezone)
                if rule_type.needs_local_resolution
                else None
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
