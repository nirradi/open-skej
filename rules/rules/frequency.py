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

**Every boundary here is a UTC boundary.** ``interfaces.py`` rejects a non-zero offset at
construction, so a week or a month starts at UTC midnight and there are no DST cases: no hour is
ever skipped or repeated, and the arithmetic below is plain ``timedelta`` addition rather than
anything localised.

**The window is anchored on the request, not on ``now``.** A booking is counted against the week or
month it *falls in*, so a request three weeks out is judged against that week's bookings and not
against this one's — a limit anchored on ``now`` would refuse next month's first booking because of
this month's traffic. The practical consequence is that history reaches only as far as the window
``interfaces.history_window`` permits, so a request beyond it is measured against a history the
caller has no bookings for and passes. That is the documented bound of the engine's promise —
evaluation costs at most one calendar month of history — not a gap in these rules.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .interfaces import BaseRule, BookingRecord, BookingRequest, Context, RuleResult, Weekday

__all__ = [
    "MaxBookingsPerWeekRule",
    "MaxBookingsPerMonthRule",
]


def _format_bookings(count: int) -> str:
    """Render a booking count the way a person would say it, e.g. "1 booking", "3 bookings"."""
    return f"{count} booking" if count == 1 else f"{count} bookings"


def _day_start(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_bounds(moment: datetime, week_starts_on: Weekday) -> tuple[datetime, datetime]:
    """Return the half-open ``[start, end)`` UTC week containing ``moment``.

    ``Weekday`` is numbered to match :meth:`datetime.date.weekday`, so the offset back to the start
    of the week is a modular subtraction and holds for every choice of first day.
    """
    days_since_start = (moment.weekday() - int(week_starts_on)) % 7
    start = _day_start(moment) - timedelta(days=days_since_start)
    return start, start + timedelta(days=7)


def _month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """Return the half-open ``[start, end)`` UTC calendar month containing ``moment``."""
    start = _day_start(moment).replace(day=1)
    if start.month == 12:
        return start, start.replace(year=start.year + 1, month=1)
    return start, start.replace(month=start.month + 1)


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

    The week runs ``[start, start + 7 days)`` from ``context.calendar.week_starts_on`` at UTC
    midnight. The bound counts the request itself: with ``max_bookings=2`` and two bookings already
    in that week, the third is refused — a check on the existing count alone would allow a booking
    that takes the user one over the line.

    The boundary is half-open on both ends. A booking one second before the week starts belongs to
    the previous week and does not count; one starting exactly at the boundary instant belongs to
    this week and does.
    """

    def __init__(self, max_bookings: int) -> None:
        if max_bookings <= 0:
            raise ValueError(
                f"MaxBookingsPerWeekRule.max_bookings must be positive; got {max_bookings!r}"
            )
        self.max_bookings = max_bookings

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        lower, upper = _week_bounds(request.start_at, context.calendar.week_starts_on)
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

    As with the weekly rule the bound counts the request itself, and the window is half-open, so a
    booking at ``00:00`` on the first of the month belongs to that month and one a second earlier
    does not.
    """

    def __init__(self, max_bookings: int) -> None:
        if max_bookings <= 0:
            raise ValueError(
                f"MaxBookingsPerMonthRule.max_bookings must be positive; got {max_bookings!r}"
            )
        self.max_bookings = max_bookings

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        lower, upper = _month_bounds(request.start_at)
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
