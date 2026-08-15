"""The hand-written canon: seven rules, four of which form ``DEFAULT_CANON``.

These are hand-written, not generated. They are the reference the AI generation loop is measured
against, so they are also the worked example of what a rule looks like: parameters on the instance,
a single ``evaluate`` that is a pure function of ``(request, context)``, and ``fail_reason`` copy
that a person can act on.

**Parameters live on the instance, never as module constants.** A Space that allows 45-minute
bookings and one that allows two hours are the same rule with different arguments; per-Space
configuration then becomes a change to how the canon is built rather than a change to any rule.
``DEFAULT_CANON`` supplies the literal values in force today.

**Every datetime here is UTC**, per ``interfaces.py``, which rejects a non-zero offset at
construction. :class:`AvailabilityHoursRule` is the one exception worth calling out for the
opposite reason: it takes no datetime at all. ``opens_at_minutes`` / ``closes_at_minutes`` are
plain integers, minutes from the venue's own **local** midnight, read off ``context.local`` — the
adapter is the only thing that knows a timezone (``.claude/rules/rule-engine.md``), so by the time
this rule runs, "local" has already been reduced to two numbers with nothing left to convert. Its
denial copy still names no bound at all: even a local clock time is a fact about one specific
Space's configuration, not something worth hardcoding into copy shared by the whole canon.

**``SessionLengthRule`` is the one rule missing from ``DEFAULT_CANON``, for the same reason
``SlotAlignmentRule`` — the type it replaces
(``ops/pending/bugs/grid-from-hours-and-min-duration.md``) — always was.** Every other rule here
takes a literal that means the same thing on every date it is asked about — a duration, a day
count, a clock time. ``SessionLengthRule`` takes an ``anchor_minutes`` that is inherently date-bound
(the date's own resolved opening time, or local midnight when no ``availability_hours`` row governs
it), so a literal baked into ``DEFAULT_CANON`` at import time would be correct for the day it was
written and silently wrong the day an admin changed the Space's hours — precisely the cached-offset
bug ``CLAUDE.md`` warns against. It is therefore never part of ``DEFAULT_CANON``, the same way the
frequency rules in ``frequency.py`` are registered and importable but kept out of it: an adapter
resolves ``anchor_minutes`` per booking date and builds the rule fresh, never once at start-up.
"""

from __future__ import annotations

from datetime import timedelta

from .interfaces import BaseRule, BookingRequest, Context, RuleResult

__all__ = [
    "NotInThePastRule",
    "BookingHorizonRule",
    "MaxDurationRule",
    "MaxConsecutiveDurationRule",
    "SessionLengthRule",
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


class SessionLengthRule(BaseRule):
    """Bookings must start and end on the ``session_minutes`` grid anchored at ``anchor_minutes``.

    Replaces both ``SlotAlignmentRule`` and ``MinDurationRule``
    (``ops/pending/bugs/grid-from-hours-and-min-duration.md``): if a booking's start and end both
    land on the grid, the shortest possible booking is one session, so a separate floor is
    redundant and goes. Unlike ``SlotAlignmentRule`` this works entirely in ``LocalFrame`` minutes,
    the way ``AvailabilityHoursRule`` already does — reading ``context.local.start_minutes`` /
    ``end_minutes`` against ``anchor_minutes``, never a UTC instant. There is no UTC anchor instant
    to grow stale here, which is what kept ``SlotAlignmentRule`` out of ``DEFAULT_CANON`` (see the
    module docstring) — that problem does not exist for this rule's *evaluation*, only for
    resolving ``anchor_minutes`` itself, which is still a per-date adapter concern.

    ``anchor_minutes`` is resolved by the adapter as the date's own opening time — never a stored
    param — falling back to local midnight when no ``availability_hours`` row governs the date.
    Anchoring on the venue's own opening time, rather than always local midnight, is the entire
    point of this rule replacing ``SlotAlignmentRule``.

    Both bounds are checked independently and **both must land on the grid**: a booking with an
    aligned start and an off-grid end is denied on the end just as an off-grid start is, since the
    calendar this Space renders has no row for either.

    Python's ``%`` is non-negative for a negative left operand, so a start before the anchor lands
    on the same extended grid with no special case: ``(minutes - anchor_minutes) % session_minutes``
    is correct whether ``minutes`` falls before or after the anchor.
    """

    def __init__(self, session_minutes: int, anchor_minutes: int) -> None:
        if session_minutes <= 0:
            raise ValueError(
                f"SessionLengthRule.session_minutes must be positive; got {session_minutes!r}"
            )
        if 1440 % session_minutes != 0:
            raise ValueError(
                "SessionLengthRule.session_minutes must divide 1440 (a whole number of sessions "
                f"per day); got {session_minutes!r}"
            )
        if not (0 <= anchor_minutes < 1440):
            raise ValueError(
                "SessionLengthRule.anchor_minutes must be within a single local day "
                f"(0 <= anchor_minutes < 1440); got {anchor_minutes!r}"
            )
        self.session_minutes = session_minutes
        self.anchor_minutes = anchor_minutes

    def _on_grid(self, minutes: int) -> bool:
        return (minutes - self.anchor_minutes) % self.session_minutes == 0

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        frame = context.local
        if not self._on_grid(frame.start_minutes):
            return RuleResult.deny(
                f"Bookings run in whole {self.session_minutes}-minute sessions counted from"
                " opening time. This one does not start on a session boundary."
            )
        if not self._on_grid(frame.end_minutes):
            return RuleResult.deny(
                f"Bookings run in whole {self.session_minutes}-minute sessions counted from"
                " opening time. This one does not end on a session boundary."
            )
        return RuleResult.allow()


class AvailabilityHoursRule(BaseRule):
    """Bookings must sit inside the recurring **local** daily window
    ``[opens_at_minutes, closes_at_minutes)``, both counted from the venue's own local midnight.

    This reads ``context.local`` exclusively — ``frame.start_minutes`` and ``frame.end_minutes`` —
    and touches no datetime and no clock time at all. There is no UTC calendar day here to cross,
    and no occurrence to reconstruct from a bare ``time`` with the date stripped off: the adapter
    has already reduced "local" to two plain integers before this rule ever runs
    (``.claude/rules/rule-engine.md``).

    **``closes_at_minutes`` may exceed 1440**, and that is not an edge case, it is the entire reason
    this rule no longer speaks UTC: a window open past its own local midnight — 18:00 to 02:00 — is
    ``closes_at_minutes = 1560``, plainly. The closing bound is **inclusive**: ending exactly at
    closing time is fine. The constructor enforces ``0 <= opens_at_minutes < 1440`` and
    ``opens_at_minutes < closes_at_minutes <= opens_at_minutes + 1440`` — a window is at most 24
    hours long and always starts somewhere on the day it is configured for, so a caller cannot typo
    an inversion into an unboundedly long or backwards window the way a bare pair of clock times
    once could.

    ``evaluate`` still has to choose between at most two candidate occurrences of the recurring
    window, exactly as the old UTC-clock-time version did — a request whose local day starts just
    after local midnight can fall in *today's* occurrence (if it opens at minute 0 or later and the
    request starts before it closes) or in the **tail of yesterday's** occurrence, shifted back by a
    full day (``closes_at_minutes - 1440``), when the window crossed midnight. Those two candidate
    ranges never overlap — the constructor's own bound (``closes_at_minutes <= opens_at_minutes +
    1440``) guarantees ``closes_at_minutes - 1440 <= opens_at_minutes`` — so at most one of them can
    ever contain ``frame.start_minutes``, and if neither does, the request sits in the daily gap
    between one closing and the next opening.
    """

    def __init__(self, opens_at_minutes: int, closes_at_minutes: int) -> None:
        if not (0 <= opens_at_minutes < 1440):
            raise ValueError(
                "AvailabilityHoursRule.opens_at_minutes must be within a single local day "
                f"(0 <= opens_at_minutes < 1440); got {opens_at_minutes!r}"
            )
        if not (opens_at_minutes < closes_at_minutes <= opens_at_minutes + 1440):
            raise ValueError(
                "AvailabilityHoursRule.closes_at_minutes must be after opens_at_minutes and at "
                f"most 24 hours later; got opens_at_minutes={opens_at_minutes!r}, "
                f"closes_at_minutes={closes_at_minutes!r}"
            )
        self.opens_at_minutes = opens_at_minutes
        self.closes_at_minutes = closes_at_minutes

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        frame = context.local
        opens = self.opens_at_minutes
        closes = self.closes_at_minutes

        if frame.start_minutes >= opens:
            # Today's own occurrence — the only one there is when the window doesn't cross local
            # midnight, and the "opens today" half when it does.
            closing_bound = closes
        elif closes > 1440 and frame.start_minutes < closes - 1440:
            # The tail of the occurrence that opened on the *previous* local day.
            closing_bound = closes - 1440
        else:
            return RuleResult.deny(
                "That's before we open, so this booking starts too early."
                " Please check this Space's opening hours on the calendar and pick a later time."
            )

        if frame.end_minutes > closing_bound:
            return RuleResult.deny(
                "That's after we close, so this booking runs too late."
                " Please check this Space's opening hours on the calendar and pick an earlier"
                " time."
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
        AvailabilityHoursRule(opens_at_minutes=6 * 60, closes_at_minutes=23 * 60),
    )


#: The canon as a ready-made value, for callers that want it without a call.
DEFAULT_CANON = default_canon()
