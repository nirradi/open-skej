"""The backend's adapter onto the rule engine.

The name is historical: this module began as a stub standing in for the engine
before it existed. It is now a thin **adapter** — it owns no rule logic. Every
verdict comes from ``rules.evaluate_request`` running ``DEFAULT_CANON``.

Callers are unchanged by that swap and must stay that way: they build a
``BookingRequest``, optionally a ``Context``, and read ``RuleResult.allowed`` /
``RuleResult.message``. Those three pydantic models are this module's public
surface, deliberately distinct from the engine's frozen dataclasses of the same
names — the API boundary validates untrusted input from the wire, the engine
boundary enforces UTC and the history window. This module is where the two meet.

Three translations happen here and nowhere else:

* **Timezone.** The engine rejects a non-zero UTC offset outright; this boundary
  accepts any aware datetime and converts. See ``_to_utc``.
* **The allow-path message.** ``RuleResult(passed=True)`` carries no copy by
  design, but the API shows friendly text on success. ``ALLOWED_MESSAGE`` is
  supplied here.
* **History.** See ``_engine_context``.

``message`` is shown verbatim to an end user, so it must stay friendly and
human-readable — never an error code or an exception repr. The engine's copy is
already written to that standard; this module does not reword it.
"""

from datetime import datetime, time, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rules import (
    DEFAULT_CANON,
    AvailabilityHoursRule,
    BookingHorizonRule,
    CalendarContext,
    HistoryContext,
    MaxDurationRule,
    UserContext,
    Weekday,
    evaluate_request,
)
from rules import BookingRequest as EngineBookingRequest
from rules import Context as EngineContext

from app.db.constants import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID

ALLOWED_MESSAGE = "Looks good — this slot is available."

#: The week convention handed to ``CalendarContext``, which requires one.
#:
#: No rule in ``DEFAULT_CANON`` reads it — the rules that do (``MaxBookingsPerWeekRule``)
#: are deliberately not in the default canon. It is stated rather than defaulted because
#: the day a counting rule joins the canon, the value it counts against must already be a
#: decision somebody made, not whatever the constructor happened to pick.
WEEK_STARTS_ON = Weekday.MONDAY


def _canon_rule(rule_type: type):
    """Return the single instance of ``rule_type`` in ``DEFAULT_CANON``.

    The constants below are read off the canon rather than restated as literals. The
    canon owns these numbers; a second copy here would be one nobody updates, and this
    module's values are mirrored by ``app/e2e/tests/03-sad-path.spec.ts`` and asserted by
    the backend suite — so a drift would show up as a passing test against a wrong number.
    """
    for rule in DEFAULT_CANON:
        if isinstance(rule, rule_type):
            return rule
    raise RuntimeError(f"DEFAULT_CANON no longer contains a {rule_type.__name__}")


MAX_BOOKING_DURATION: timedelta = _canon_rule(MaxDurationRule).max_duration
AVAILABILITY_OPEN: time = _canon_rule(AvailabilityHoursRule).opens_at
AVAILABILITY_CLOSE: time = _canon_rule(AvailabilityHoursRule).closes_at
BOOKING_HORIZON_DAYS: int = _canon_rule(BookingHorizonRule).days


class BookingRequest(BaseModel):
    """The booking a user is asking for. Times are timezone-aware.

    Any aware datetime is accepted, at any offset. The engine's own
    ``BookingRequest`` accepts UTC only; ``_to_utc`` converts at the call, so a
    client that sends ``+02:00`` is served rather than rejected.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str = DEFAULT_USER_ID
    resource_id: str = DEFAULT_RESOURCE_ID
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


class Context(BaseModel):
    """Everything a rule may consult beyond the request itself.

    ``now`` is the clock the time-relative rules judge against. It lives here
    rather than being read inline with ``datetime.now()`` for two reasons: rules
    stay pure functions of ``(request, context)``, and tests can pin it instead
    of racing the wall clock. It defaults to the current UTC instant, so no
    existing caller has to pass it.

    ``history`` is the requesting user's prior bookings. **No rule in the canon
    in force today reads it**, and it is not forwarded to the engine — see
    ``_engine_context``. The field is kept because the parameter is part of this
    module's published shape and callers already pass it.
    """

    model_config = ConfigDict(frozen=True)

    history: tuple[BookingRequest, ...] = Field(default=())
    now: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _check_now_is_aware(self) -> "Context":
        if self.now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return self


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
    """
    return value.astimezone(timezone.utc)


def _engine_request(booking: BookingRequest) -> EngineBookingRequest:
    return EngineBookingRequest(
        user_id=booking.user_id,
        resource_id=booking.resource_id,
        start_at=_to_utc(booking.start_at),
        end_at=_to_utc(booking.end_at),
    )


def _engine_context(booking: BookingRequest, context: Context) -> EngineContext:
    """Build the engine's ``Context``, with **empty history**.

    That emptiness is a decision, not an omission. ``DEFAULT_CANON`` holds the four
    request-local rules only; the rules that count history — ``MaxBookingsPerWeekRule``,
    ``MaxBookingsPerMonthRule`` — are deliberately excluded from it. So there is no
    history to fetch and no query to run, and issuing one would cost a round trip per
    booking attempt to build an argument nothing reads.

    When a counting rule joins the canon, this is the function that changes: it must
    load the user's bookings for this resource, capped to ``rules.history_window(now)``,
    and pass them as ``BookingRecord``s. The engine re-asserts that cap, so a query that
    reaches too far fails loudly here rather than silently widening what a rule may know.

    ``Context.history`` on this module's own model is dropped for the same reason. It is
    populated by callers written against the old stub and is not silently *misused* — it
    reaches no rule at all.
    """
    return EngineContext(
        user=UserContext(user_id=booking.user_id),
        calendar=CalendarContext(week_starts_on=WEEK_STARTS_ON, now=_to_utc(context.now)),
        history=HistoryContext(),
    )


def evaluate(booking: BookingRequest, context: Context | None = None) -> RuleResult:
    """Run the canon against the request and return the first denial.

    ``context`` defaults to an empty one reading the live clock, so a caller with
    nothing to supply still gets a valid result.

    ``ContextMismatchError`` is not caught. Both the request and the context are
    built here from the same ``booking``, so a mismatch cannot be a client error —
    it would be a bug in this adapter, and the engine raises precisely so that it
    reaches the error tracker instead of being served as a polite refusal. Every
    other failure inside a rule is already contained by the controller and arrives
    as an ordinary denial.
    """
    context = context if context is not None else Context()

    result = evaluate_request(
        _engine_request(booking),
        _engine_context(booking, context),
        DEFAULT_CANON,
    )

    if result.passed:
        # The engine drops the message on the allow path (``passed=True`` implies
        # ``fail_reason is None``); the success banner is this layer's copy to write.
        return RuleResult(allowed=True, message=ALLOWED_MESSAGE)
    return RuleResult(allowed=False, message=result.fail_reason)
