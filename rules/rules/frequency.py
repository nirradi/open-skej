"""Hand-written canon rules that count a user's bookings over a calendar window.

A sibling of ``canon.py`` rather than part of it: these two rules are the first that read
``context.history``, and they are the only ones whose verdict depends on anything beyond the request
itself. They are also **not in** ``DEFAULT_CANON`` — see the note at the bottom of this module.

Written by hand, not generated, for the same reason as the rest of the canon: they are the
reference the generation loop is measured against, and "max 2 times a week" is the golden example
the loop is expected to reproduce.

**Everything in ``HistoryContext`` counts.** No status is inspected and none exists to inspect —
``BookingRecord`` has no such field. A booking that should not count toward a limit is one the
caller does not put in the context. The engine stays ignorant of a schema that will keep changing;
a rule that filtered internally would silently mis-enforce the day a ``deleted`` or ``no_show`` flag
appeared, with nothing to signal that it had.

**These rules are handed their window; they do not derive it.** Both take a half-open
``[window_start, window_end)`` pair of UTC instants at construction. They used to compute the week
or month themselves by snapping the request to UTC midnight, which silently miscounted for every
venue not sitting on UTC: a Sydney booking at 00:30 on Monday local is 13:30 **Sunday** in UTC, so
it landed in the previous UTC week and a weekly cap of one admitted a second booking in the same
Sydney week. A local week has no fixed UTC representation, and the engine has no timezone to find
one with — so the caller, which does, resolves the boundary and passes the instants in. That is the
same "conversion happens at the boundary" split availability hours already follow.

The bounds are still **UTC instants** and everything below is still plain instant comparison. What
moved out is the decision about *which* instants; the engine remains zone-free, and no rule here
reads a wall clock.

**The window is anchored on the request, not on ``now``.** A booking is counted against the week or
month it *falls in*, so a request three weeks out is judged against that week's bookings and not
against this one's — a limit anchored on ``now`` would refuse next month's first booking because of
this month's traffic. That property now lives in whoever computes the bounds, and it is a property
of the *caller* rather than of these rules; ``app.rules_stub`` resolves both windows from the
booking's own local date for exactly this reason. The practical consequence is unchanged: history
reaches only as far as ``interfaces.history_window`` permits, so a request beyond it is measured
against a history the caller has no bookings for and passes. That is the documented bound of the
engine's promise — evaluation costs at most one calendar month of history — not a gap in these
rules.
"""

from __future__ import annotations

from datetime import datetime

from .interfaces import (
    BaseRule,
    BookingRecord,
    BookingRequest,
    Context,
    RuleResult,
    _require_utc,
)

__all__ = [
    "MaxBookingsPerWeekRule",
    "MaxBookingsPerMonthRule",
]


def _format_bookings(count: int) -> str:
    """Render a booking count the way a person would say it, e.g. "1 booking", "3 bookings"."""
    return f"{count} booking" if count == 1 else f"{count} bookings"


def _validate_window(
    window_start: object, window_end: object, rule_name: str
) -> tuple[datetime, datetime]:
    """Validate a half-open ``[window_start, window_end)`` pair of UTC instants.

    The same zero-offset discipline ``interfaces.py`` applies to every datetime crossing this
    boundary, applied to the one pair that now arrives through a constructor rather than through a
    dataclass. A caller that resolved a local week against the wrong zone cannot be caught here —
    but one that forgot to convert to UTC at all can be, and that is the likelier mistake.
    """
    start = _require_utc(window_start, f"{rule_name}.window_start")
    end = _require_utc(window_end, f"{rule_name}.window_end")
    if start >= end:
        raise ValueError(
            f"{rule_name}.window_start must be strictly before window_end; got {start} >= {end}"
        )
    return start, end


def _count_starting_within(
    bookings: tuple[BookingRecord, ...], lower: datetime, upper: datetime
) -> int:
    """Count bookings whose ``start_at`` lies in the half-open interval ``[lower, upper)``.

    A booking belongs to the window it **starts** in, and to exactly one window: an interval that
    straddles midnight on a boundary would otherwise be counted twice, once against each side.
    """
    return sum(1 for booking in bookings if lower <= booking.start_at < upper)


class MaxBookingsPerWeekRule(BaseRule):
    """A user may hold at most ``max_bookings`` bookings in the week the request falls in.

    The week is ``[window_start, window_end)``, a pair of UTC instants supplied by the caller. It is
    *not* derived here: which instants bound "the week this booking is in" depends on the venue's
    timezone, and the engine has none — see the module docstring for the miscount that followed from
    deriving it. The caller is expected to resolve the week containing the **request**, in the
    venue's own zone, honouring whatever first-day-of-week convention applies.

    The bound counts the request itself: with ``max_bookings=2`` and two bookings already in that
    week, the third is refused — a check on the existing count alone would allow a booking that
    takes the user one over the line.

    The boundary is half-open. A booking one second before the window starts belongs to the previous
    week and does not count; one starting exactly at the boundary instant belongs to this week and
    does.
    """

    def __init__(self, max_bookings: int, window_start: datetime, window_end: datetime) -> None:
        if max_bookings <= 0:
            raise ValueError(
                f"MaxBookingsPerWeekRule.max_bookings must be positive; got {max_bookings!r}"
            )
        self.max_bookings = max_bookings
        self.window_start, self.window_end = _validate_window(
            window_start, window_end, "MaxBookingsPerWeekRule"
        )

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        lower, upper = self.window_start, self.window_end
        existing = _count_starting_within(context.history.bookings, lower, upper)
        if existing + 1 > self.max_bookings:
            return RuleResult.deny(
                f"You can make at most {_format_bookings(self.max_bookings)} a week,"
                f" and you already have {_format_bookings(existing)} that week."
                " Please pick a time in another week."
            )
        return RuleResult.allow()


class MaxBookingsPerMonthRule(BaseRule):
    """A user may hold at most ``max_bookings`` bookings in the calendar month the request falls in.

    Calendar months, not rolling 30-day windows: the limit a user is told about is the one they can
    count on a calendar. December rolls into January like any other boundary — a December booking
    does not count toward January's allowance.

    As with the weekly rule the month is ``[window_start, window_end)``, supplied by the caller
    rather than derived, for the same reason: "the calendar month this booking is in" is a question
    about the venue's local calendar, and the last local evening of a month is already the next
    month in UTC for any venue far enough ahead of it.

    The bound counts the request itself, and the window is half-open, so a booking at the opening
    instant belongs to that month and one a second earlier does not.
    """

    def __init__(self, max_bookings: int, window_start: datetime, window_end: datetime) -> None:
        if max_bookings <= 0:
            raise ValueError(
                f"MaxBookingsPerMonthRule.max_bookings must be positive; got {max_bookings!r}"
            )
        self.max_bookings = max_bookings
        self.window_start, self.window_end = _validate_window(
            window_start, window_end, "MaxBookingsPerMonthRule"
        )

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        lower, upper = self.window_start, self.window_end
        existing = _count_starting_within(context.history.bookings, lower, upper)
        if existing + 1 > self.max_bookings:
            return RuleResult.deny(
                f"You can make at most {_format_bookings(self.max_bookings)} a month,"
                f" and you already have {_format_bookings(existing)} that month."
                " Please pick a time in another month."
            )
        return RuleResult.allow()


# Deliberately absent from ``DEFAULT_CANON``. The four rules in ``canon.py`` are what Stream 1's
# end-to-end suite asserts against, and adding a booking limit to the default canon would change
# behaviour those tests depend on at integration. These two are exported for a caller that wants
# them; wiring them into a canon is a later task's decision, alongside per-Space configuration.
