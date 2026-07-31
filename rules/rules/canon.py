"""The hand-written canon: the four rules every Space enforces.

These are hand-written, not generated. They are the reference the AI generation loop is measured
against, so they are also the worked example of what a rule looks like: parameters on the instance,
a single ``evaluate`` that is a pure function of ``(request, context)``, and ``fail_reason`` copy
that a person can act on.

**Parameters live on the instance, never as module constants.** A Space that allows 45-minute
bookings and one that allows two hours are the same rule with different arguments; per-Space
configuration then becomes a change to how the canon is built rather than a change to any rule.
``DEFAULT_CANON`` supplies the literal values in force today.

**Every datetime here is UTC**, per ``interfaces.py``, which rejects a non-zero offset at
construction. This bears directly on :class:`AvailabilityHoursRule`: ``opens_at`` and ``closes_at``
are **UTC clock times**, and ``start_at.time()`` is a UTC wall clock. A Space whose doors open at
06:00 local does not open at ``time(6, 0)`` here unless it happens to sit on UTC. Rendering those
bounds in a viewer's own timezone is the UI's job; the engine has no timezone to convert from and
deliberately gains no DST cases.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .interfaces import BaseRule, BookingRequest, Context, RuleResult

__all__ = [
    "NotInThePastRule",
    "BookingHorizonRule",
    "MaxDurationRule",
    "AvailabilityHoursRule",
    "DEFAULT_CANON",
    "default_canon",
]


def _format_duration(duration: timedelta) -> str:
    """Render a duration the way a person would say it, e.g. "2 hours".

    The exact output is contract, not cosmetics: it is interpolated into user-facing copy that the
    end-to-end suite asserts as a full-string match.
    """
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


class NotInThePastRule(BaseRule):
    """Bookings may not start before ``context.now``.

    The bound is **inclusive of the present instant**: a booking starting exactly now is allowed,
    one starting a minute ago is not. Only ``start_at`` is tested — a booking already under way is
    out of bounds regardless of when it ends, and ``end_at`` is guaranteed to be later anyway.
    """

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        if request.start_at < context.now:
            return RuleResult.deny(
                "That time has already passed, so it can't be booked."
                " Please pick a time in the future."
            )
        return RuleResult.allow()


class BookingHorizonRule(BaseRule):
    """Bookings may not start more than ``days`` ahead of ``context.now``.

    The bound is **inclusive**: exactly ``days`` ahead is the last bookable instant. Measured from
    ``start_at`` only, so a booking that begins inside the horizon is fine even if it runs a little
    past it — the alternative would refuse a legitimate booking for the sake of its final minutes.
    """

    def __init__(self, days: int) -> None:
        if days <= 0:
            raise ValueError(f"BookingHorizonRule.days must be positive; got {days!r}")
        self.days = days

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        horizon = context.now + timedelta(days=self.days)
        if request.start_at > horizon:
            return RuleResult.deny(
                f"Bookings can only be made up to {self.days} days ahead,"
                " and this one is further out than that."
                " Please pick an earlier date."
            )
        return RuleResult.allow()


class MaxDurationRule(BaseRule):
    """Bookings may not run longer than ``max_duration``.

    The bound is inclusive: a booking of exactly ``max_duration`` passes.
    """

    def __init__(self, max_duration: timedelta) -> None:
        if max_duration <= timedelta(0):
            raise ValueError(f"MaxDurationRule.max_duration must be positive; got {max_duration!r}")
        self.max_duration = max_duration

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        if request.duration > self.max_duration:
            return RuleResult.deny(
                f"Bookings can be at most {_format_duration(self.max_duration)} long,"
                f" and this one is {_format_duration(request.duration)}."
                " Please shorten it and try again."
            )
        return RuleResult.allow()


class AvailabilityHoursRule(BaseRule):
    """Bookings must sit inside the recurring daily window ``[opens_at, closes_at]``.

    ``opens_at`` and ``closes_at`` are **UTC** clock times — see the module docstring. The closing
    bound is **inclusive**: ending exactly at closing time is fine.

    **The window may cross a UTC calendar day**, and ``opens_at`` is not required to be before
    ``closes_at`` — when it isn't, that inversion *is* the window: it opens on one UTC date and
    closes on the next. This is not the same thing as a venue open past its own *local* midnight
    (``DEFERRED.md`` item 18, unsupported and unrelated): an entirely ordinary same-local-day window
    — Sydney 09:00-21:00, Honolulu 08:00-20:00 — resolves to exactly this shape in UTC whenever the
    zone's offset is large enough that local morning falls on the UTC day before (``app.
    operating_hours``, which is where such a pair comes from and why it stopped raising rather than
    returning one). A same-Space, same-UTC-day window (Berlin, or the reference hours below) simply
    never inverts, so this rule behaves exactly as it always did for the case everyone has been
    testing against.

    ``evaluate`` never anchors ``opens_at`` and ``closes_at`` to ``request.start_at.date()``
    independently of each other — doing that is what silently mislabelled a denial's reason
    (``DEFERRED.md`` item 16): for a crossing window, "the UTC date ``start_at`` falls on" is not a
    fact about the window, it's a fact about *which half* of one occurrence the request landed in.
    ``_occurrence_for`` decides that once, from ``start_at`` alone, and both bounds of what it
    returns come from that same decision — never one built on an assumption the other contradicts.
    """

    def __init__(self, opens_at: time, closes_at: time) -> None:
        if opens_at == closes_at:
            raise ValueError(
                f"AvailabilityHoursRule.opens_at must differ from closes_at; both were {opens_at}"
            )
        self.opens_at = opens_at
        self.closes_at = closes_at

    def _occurrence_for(self, start_at: datetime) -> tuple[datetime, datetime] | None:
        """The one occurrence of the recurring window that could contain ``start_at``.

        ``None`` means ``start_at`` falls in the daily gap between one closing and the next
        opening — outside every occurrence, whatever ``end_at`` turns out to be.
        """
        tzinfo = start_at.tzinfo
        start_date = start_at.date()

        if self.opens_at < self.closes_at:
            # Same UTC day: the only occurrence a `start_at` on this date could belong to.
            return (
                datetime.combine(start_date, self.opens_at, tzinfo),
                datetime.combine(start_date, self.closes_at, tzinfo),
            )
        if start_at.time() >= self.opens_at:
            # The "opens today" half of an occurrence that closes on the following UTC date.
            return (
                datetime.combine(start_date, self.opens_at, tzinfo),
                datetime.combine(start_date + timedelta(days=1), self.closes_at, tzinfo),
            )
        if start_at.time() < self.closes_at:
            # The "closes today" half of an occurrence that opened on the previous UTC date.
            return (
                datetime.combine(start_date - timedelta(days=1), self.opens_at, tzinfo),
                datetime.combine(start_date, self.closes_at, tzinfo),
            )
        return None

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        friendly_hours = f"between {_format_time(self.opens_at)} and {_format_time(self.closes_at)}"
        occurrence = self._occurrence_for(request.start_at)

        if occurrence is None or request.start_at < occurrence[0]:
            return RuleResult.deny(
                f"We open at {_format_time(self.opens_at)}, so this booking starts too early."
                f" Please pick a time {friendly_hours}."
            )

        _, closing = occurrence
        if request.end_at > closing:
            return RuleResult.deny(
                f"We close at {_format_time(self.closes_at)}, so this booking runs too late."
                f" Please pick a time {friendly_hours}."
            )

        return RuleResult.allow()


def default_canon() -> tuple[BaseRule, ...]:
    """Build the canon in force today, in the order the controller runs it.

    **The order arbitrates copy.** ``evaluate_request`` is fail-fast, so the first rule to deny
    decides the single message a user sees when a request breaks several rules at once.

    The two date rules run first: they reject a booking on *when* it is, which no amount of
    shortening or shifting within the day can fix. Telling someone to trim a 3-hour booking that
    sits 90 days out would send them to fix the one thing that isn't the problem — they would
    shorten it, resubmit, and be refused again. Duration and availability hours are both remedies
    the user can apply to a date that is otherwise bookable, so they come after.

    Past and horizon are mutually exclusive, so their relative order never actually arbitrates a
    message; past is first only because it reads chronologically.
    """
    return (
        NotInThePastRule(),
        BookingHorizonRule(days=60),
        MaxDurationRule(max_duration=timedelta(hours=2)),
        AvailabilityHoursRule(opens_at=time(6, 0), closes_at=time(23, 0)),
    )


#: The canon as a ready-made value, for callers that want it without a call.
DEFAULT_CANON = default_canon()
