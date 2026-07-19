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

from datetime import datetime, time, timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.constants import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID

# The stub's rule parameters. Stream 3 makes these per-Space configuration; here
# they are module-level constants so the numbers never appear inline in the logic
# or in the messages built from them.
MAX_BOOKING_DURATION = timedelta(hours=2)
AVAILABILITY_OPEN = time(6, 0)
AVAILABILITY_CLOSE = time(23, 0)

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
    """

    model_config = ConfigDict(frozen=True)

    history: tuple[BookingRequest, ...] = Field(default=())


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


# The active rule canon, run in order. Stream 3 fetches this list per Space
# instead of hardcoding it.
RULES = (_check_max_duration, _check_availability_hours)


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
