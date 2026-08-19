"""The hand-written canon: four rules, three of which form ``DEFAULT_CANON``.

These are hand-written, not generated. They are the reference the AI generation loop is measured
against, so they are also the worked example of what a rule looks like: parameters on the instance,
a single ``evaluate`` that is a pure function of ``(request, context)``, and ``fail_reason`` copy
that a person can act on.

**Parameters live on the instance, never as module constants.** A Space that allows 45-minute
bookings and one that allows two hours are the same rule with different arguments; per-Space
configuration then becomes a change to how the canon is built rather than a change to any rule.
``DEFAULT_CANON`` supplies the literal values in force today.

**Every datetime here is UTC**, per ``interfaces.py``, which rejects a non-zero offset at
construction.

``SessionLengthRule`` and ``AvailabilityHoursRule`` are retired — the calendar shape
(``rules/shape/``, ``.claude/rules/calendar-shape.md``) now says everything they said, checked
structurally by the booking gate before this canon ever runs, so leaving them registered here would
leave an admin two places to configure hours that could disagree.

**``MaxConsecutiveDurationRule`` is the one rule remaining missing from ``DEFAULT_CANON``.**
``DEFAULT_CANON`` is frozen at the reference values the generation loop is measured against and the
end-to-end suite asserts on; a rule type added since has stayed out of it rather than changing what
those existing assertions cover. It is registered and importable — ``rules.frequency``'s types share
the identical treatment — and a per-Space canon assembled from ``space_rules`` rows (the adapter,
not this module) includes it whenever a Space configures one.
"""

from __future__ import annotations

from datetime import timedelta

from .interfaces import BaseRule, BookingRequest, Context, RuleResult

__all__ = [
    "NotInThePastRule",
    "BookingHorizonRule",
    "MaxDurationRule",
    "MaxConsecutiveDurationRule",
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


class MaxConsecutiveDurationRule(BaseRule):
    """Caps the contiguous **run** of back-to-back bookings this request joins — never the request's
    own span, which is what ``MaxDurationRule`` immediately above judges instead.

    That distinction is the entire reason this rule exists
    (``ops/pending/bugs/max-duration-cannon.md``): a Space configured "max 2 hours" meaning *one
    session* is served correctly by ``MaxDurationRule``, which reads ``request.duration`` and has
    no way to see anything either side of the request. A Space that meant "no more than 2 hours of
    court time in a row" was not served at all — a member booking 17:00-18:00 and then, separately,
    18:00-19:00 passes ``MaxDurationRule`` twice and walks away with four hours nobody configured.
    This rule is that missing reading: it denies when ``context.run.duration`` — the whole
    contiguous span the request joins, resolved by the adapter from this user's own history across
    every Resource in the Space (``RunContext``) — exceeds ``max_duration``, whether or not the
    request's own span does.

    A Space can configure either, both, or neither; they never disagree about the same booking. A
    run always contains its own request, so an inclusive bound on the run can never pass a request
    that the identical bound on ``request.duration`` alone would already have denied — adding this
    rule to a Space that already has ``max_duration`` can only refuse bookings that rule was already
    silent on, never change what it already refuses.

    The bound is inclusive, the same convention every duration rule in this canon shares: a run of
    exactly ``max_duration`` passes, one minute over does not.
    """

    def __init__(self, max_duration: timedelta) -> None:
        if max_duration <= timedelta(0):
            raise ValueError(
                f"MaxConsecutiveDurationRule.max_duration must be positive; got {max_duration!r}"
            )
        self.max_duration = max_duration

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        if context.run.duration > self.max_duration:
            return RuleResult.deny(
                f"Bookings can't add up to more than {_format_duration(self.max_duration)} of"
                " consecutive play back-to-back, and joining this one to what you already have"
                f" booked next to it would come to {_format_duration(context.run.duration)}."
                " Please leave a gap before or after it, or shorten it, and try again."
            )
        return RuleResult.allow()


def default_canon() -> tuple[BaseRule, ...]:
    """Build the canon in force today, in the order the controller runs it.

    **The order arbitrates copy.** ``evaluate_request`` is fail-fast, so the first rule to deny
    decides the single message a user sees when a request breaks several rules at once.

    The date rule runs first: it rejects a booking on *when* it is, which no amount of shortening
    can fix. Telling someone to trim a 3-hour booking that sits 90 days out would send them to fix
    the one thing that isn't the problem — they would shorten it, resubmit, and be refused again.
    Duration is a remedy the user can apply to a date that is otherwise bookable, so it comes after.

    Past and horizon are mutually exclusive, so their relative order never actually arbitrates a
    message; past is first only because it reads chronologically.
    """
    return (
        NotInThePastRule(),
        BookingHorizonRule(days=60),
        MaxDurationRule(max_duration=timedelta(hours=2)),
    )


#: The canon as a ready-made value, for callers that want it without a call.
DEFAULT_CANON = default_canon()
