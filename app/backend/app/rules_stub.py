"""A stubbed stand-in for the Stream 3 rule engine.

Stream 1 needs *a* rule engine to prove the end-to-end happy/sad path before the
real one exists. This module implements the interface described in
``.claude/rules/stream-3-rules.md`` — the ``BookingRequest`` input model, the
``Context`` input model carrying the user's booking history, the ``RuleResult``
output model, and an engine entry point that runs rules sequentially — but with a
hardcoded pair of rules instead of the real parameterized canon.

Stream 3 replaces the body of ``evaluate`` (or this whole module) without callers
changing: they build a ``BookingRequest``, optionally a ``Context``, and read
``RuleResult.allowed`` / ``RuleResult.message``.

``message`` is written to be shown verbatim to an end user in the UI, so it must
stay friendly and human-readable — never an error code or an exception repr.
"""

from datetime import datetime, time, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.constants import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID

# The stub's rule parameters. Stream 3 makes these per-Space configuration; here
# they are module-level constants so the numbers never appear inline in the logic
# or in the messages built from them.
MAX_BOOKING_DURATION = timedelta(hours=2)
AVAILABILITY_OPEN = time(6, 0)
AVAILABILITY_CLOSE = time(23, 0)
BOOKING_HORIZON_DAYS = 60

ALLOWED_MESSAGE = "Looks good — this slot is available."


class BookingRequest(BaseModel):
    """The booking a user is asking for. Times are timezone-aware."""

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

    ``history`` is the requesting user's bookings over the period the rules care
    about (capped at one month per the project brief). The stub's two rules are
    purely local to the request and ignore it, but rules like "no more than twice
    a week" need it, so callers should populate it and the parameter exists from
    day one — that way Stream 3 landing does not change a single call site.

    ``now`` is the clock the time-relative rules judge against. It lives here
    rather than being read inline with ``datetime.now()`` for two reasons: rules
    stay pure functions of ``(request, context)``, and tests can pin it instead
    of racing the wall clock. It defaults to the current UTC instant, so no
    existing caller has to pass it.
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
    """The engine's verdict. ``message`` is user-facing copy."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    message: str


def _format_duration(duration: timedelta) -> str:
    """Render a duration the way a person would say it, e.g. "2 hours"."""
    total_minutes = int(duration.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" if hours == 1 else f"{hours} hours")
    if minutes:
        parts.append(f"{minutes} minute" if minutes == 1 else f"{minutes} minutes")
    return " and ".join(parts) if parts else "0 minutes"


def _format_time(value: time) -> str:
    return value.strftime("%H:%M")


def _check_max_duration(booking: BookingRequest, context: Context) -> RuleResult:
    """Bookings may not run longer than MAX_BOOKING_DURATION."""
    if booking.duration > MAX_BOOKING_DURATION:
        return RuleResult(
            allowed=False,
            message=(
                f"Bookings can be at most {_format_duration(MAX_BOOKING_DURATION)} long,"
                f" and this one is {_format_duration(booking.duration)}."
                " Please shorten it and try again."
            ),
        )
    return RuleResult(allowed=True, message=ALLOWED_MESSAGE)


def _check_availability_hours(booking: BookingRequest, context: Context) -> RuleResult:
    """Bookings must sit inside [AVAILABILITY_OPEN, AVAILABILITY_CLOSE] on one day.

    Evaluated against the wall-clock times as supplied, so a request expressed in
    the space's local timezone is judged in that timezone. The closing bound is
    inclusive: ending exactly at closing time is fine. Comparing ``end_at``
    against a closing instant built from ``start_at``'s date (rather than
    comparing bare clock times) means a booking that runs past midnight is
    correctly rejected instead of wrapping around into the next open day.
    """
    friendly_hours = (
        f"between {_format_time(AVAILABILITY_OPEN)} and {_format_time(AVAILABILITY_CLOSE)}"
    )

    if booking.start_at.time() < AVAILABILITY_OPEN:
        return RuleResult(
            allowed=False,
            message=(
                f"We open at {_format_time(AVAILABILITY_OPEN)}, so this booking starts too early."
                f" Please pick a time {friendly_hours}."
            ),
        )

    closing = datetime.combine(booking.start_at.date(), AVAILABILITY_CLOSE, booking.start_at.tzinfo)
    if booking.end_at > closing:
        return RuleResult(
            allowed=False,
            message=(
                f"We close at {_format_time(AVAILABILITY_CLOSE)}, so this booking runs too late."
                f" Please pick a time {friendly_hours}."
            ),
        )

    return RuleResult(allowed=True, message=ALLOWED_MESSAGE)


def _check_not_in_the_past(booking: BookingRequest, context: Context) -> RuleResult:
    """Bookings may not start before ``context.now``.

    The bound is inclusive of the present instant: a booking starting exactly
    now is allowed, one starting a minute ago is not. Only ``start_at`` is
    tested — a booking already under way is out of bounds regardless of when it
    ends, and ``end_at`` is guaranteed to be later anyway.
    """
    if booking.start_at < context.now:
        return RuleResult(
            allowed=False,
            message=(
                "That time has already passed, so it can't be booked."
                " Please pick a time in the future."
            ),
        )
    return RuleResult(allowed=True, message=ALLOWED_MESSAGE)


def _check_within_horizon(booking: BookingRequest, context: Context) -> RuleResult:
    """Bookings may not start more than BOOKING_HORIZON_DAYS ahead of ``now``.

    The bound is inclusive: exactly BOOKING_HORIZON_DAYS ahead is the last
    bookable instant. Measured from ``start_at`` only, so a booking that begins
    inside the horizon is fine even if it runs a little past it.
    """
    horizon = context.now + timedelta(days=BOOKING_HORIZON_DAYS)
    if booking.start_at > horizon:
        return RuleResult(
            allowed=False,
            message=(
                f"Bookings can only be made up to {BOOKING_HORIZON_DAYS} days ahead,"
                " and this one is further out than that."
                " Please pick an earlier date."
            ),
        )
    return RuleResult(allowed=True, message=ALLOWED_MESSAGE)


# The active rule canon, run in order. Stream 3 fetches this list per Space
# instead of hardcoding it.
#
# Order is deliberate, because ``evaluate`` short-circuits on the first denial
# and so decides which single message a user sees when a request breaks several
# rules at once. The two date rules run first: they reject a booking on *when*
# it is, which no amount of shortening or shifting within the day can fix.
# Telling someone to trim a 3-hour booking that sits 90 days out would send them
# to fix the one thing that isn't the problem — they'd shorten it, resubmit, and
# be refused again. Duration and availability hours are both remedies the user
# can apply to a date that is otherwise bookable, so they come after. Past and
# horizon are mutually exclusive, so their relative order never actually
# arbitrates a message; past is first only because it reads chronologically.
RULES = (
    _check_not_in_the_past,
    _check_within_horizon,
    _check_max_duration,
    _check_availability_hours,
)


def evaluate(booking: BookingRequest, context: Context | None = None) -> RuleResult:
    """Run every active rule against the request and return the first denial.

    ``context`` defaults to an empty history so a caller with nothing to supply
    still gets a valid result; once Stream 3's history-aware rules land, callers
    are expected to pass a populated Context.
    """
    context = context if context is not None else Context()
    for rule in RULES:
        result = rule(booking, context)
        if not result.allowed:
            return result
    return RuleResult(allowed=True, message=ALLOWED_MESSAGE)
