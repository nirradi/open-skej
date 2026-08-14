"""Hand-written canon rules whose verdict depends on more than the request itself: three that count
a user's sessions over a window, and one that sums their total duration over one.

A sibling of ``canon.py`` rather than part of it: these are the first rules that read
``context.history``. They are also **not in** ``DEFAULT_CANON`` — see the note at the bottom of this
module.

Written by hand, not generated, for the same reason as the rest of the canon: they are the
reference the generation loop is measured against, and "max 2 times a week" is the golden example
the loop is expected to reproduce. ``rules/benchmark.py``'s ``GOLDEN_EXAMPLES`` still carries "no
more than 3 hours total per day" and "at most 2 bookings on the same day" verbatim even though
``MaxDurationPerDayRule`` / ``MaxBookingsPerDayRule`` below now ship those two constraints by hand —
that is deliberate (task 8.7, ``ops/plans/stream-8/OVERVIEW.md``): the golden examples now measure a
capability the product also ships hand-written, which is still worth benchmarking, not a redundancy
to delete.

**The three counting rules — ``MaxBookingsPerDayRule``, ``MaxBookingsPerWeekRule``,
``MaxBookingsPerMonthRule`` — count sessions, not rows (task 8.6).** Two bookings the same user
holds back to back are one session, not two (``ops/pending/bugs/max-duration-cannon.md``,
"Counting runs, not rows"): a cap of two meaning two *sessions* is not served by counting raw
booking rows, since a member who books two abutting hours would otherwise spend the whole cap on
one afternoon of continuous play. ``MaxDurationPerDayRule`` is built in the same session-counting
era but does **not** join this pattern — see its own docstring for why a *total* is the one place
in this module that deliberately sums raw entries instead.

**The merge happens inside ``evaluate``, on ``request`` and ``context.history.bookings`` together —
not ahead of time, on ``Context`` itself.** The plan this task shipped from named a preferred
mechanism first: have the adapter fold the request into the merged runs and hand this rule a
``Context.history`` that already reflects them, so the rule would do nothing but drop the old
``+1``. That mechanism conflicts with an invariant one layer up that is not visible from this
module: ``Context.__post_init__`` (``interfaces.py``) requires every entry in
``context.history.bookings`` to fall inside ``history_window(context.calendar.now)`` — the
"evaluation costs at most one calendar month of history" promise. A request folded into that field
is not bounded by that promise at all; it can be booked arbitrarily far out (limited only by
``BookingHorizonRule``, itself often configured well past a month), so folding it into
``Context.history`` raises ``ValueError`` on construction for any request beyond the window instead
of evaluating normally — breaking an ordinary future booking on *any* Space running one of these
counting rules, not merely an edge case. So the request is merged in here instead, at evaluate
time, where it is a live parameter rather than a field a shared invariant polices. This also
removes the need for the ``resource_id`` fiction the plan flagged as a real cost of the preferred
mechanism (a merged run crossing Resources has no single true one) — merging ``(start, end)`` pairs
directly needs no ``BookingRecord`` to carry a resource at all.

**``tolerance`` is what makes two rows one session.** Supplied at construction (like
``window_start``/``window_end``, below), it is the adapter's resolved gap tolerance — that date's
own minimum duration, zero when none governs it (``rules.spans.merge_adjoining_spans``,
``app.rules_stub._resolve_run``, task 8.4). ``request`` and every entry in
``context.history.bookings`` are swept together through :func:`rules.spans.merge_adjoining_spans`,
and the rule counts how many of the merged runs **start** inside its window — no ``+1``: since the
request is one of the spans the sweep already merges, a request that only extends a run the user
already holds changes nothing about which runs start where, while a request that opens a new
session adds exactly one run starting in the window, which is what pushes a count over the line.
Nothing about this rule enforces that a caller handed it real history — it trusts
``context.history.bookings`` completely, per the next paragraph — but the merge itself, unlike the
old ``+1``, cannot forget to count the request: it is always one of
the spans swept.

**A run counts on the side it begins**, matching every other window in this contract: extending a
run that started last week adds nothing to this week's count. Consistent, and mildly surprising the
first time someone meets it — but the alternative is counting one session twice.

**Everything in ``HistoryContext`` counts.** No status is inspected and none exists to inspect —
``BookingRecord`` has no such field. An entry that should not count toward a limit is one the
caller does not put in the context. The engine stays ignorant of a schema that will keep changing;
a rule that filtered internally would silently mis-enforce the day a ``deleted`` or ``no_show`` flag
appeared, with nothing to signal that it had.

**Every rule in this module is handed its window; none derives it.** Each takes a half-open
``[window_start, window_end)`` pair of UTC instants at construction. The two original rules used to
compute the week or month themselves by snapping the request to UTC midnight, which silently
miscounted for every venue not sitting on UTC: a Sydney booking at 00:30 on Monday local is 13:30
**Sunday** in UTC, so it landed in the previous UTC week and a weekly cap of one admitted a second
booking in the same Sydney week. A local week has no fixed UTC representation, and the engine has
no timezone to find one with — so the caller, which does, resolves the boundary and passes the
instants in. That is the same "conversion happens at the boundary" split availability hours
already follow.

The bounds are still **UTC instants** and everything below is still plain instant comparison. What
moved out is the decision about *which* instants; the engine remains zone-free, and no rule here
reads a wall clock.

**The window is anchored on the request, not on ``now``.** A booking is counted against the week or
month it *falls in*, so a request three weeks out is judged against that week's bookings and not
against this one's — a limit anchored on ``now`` would refuse next month's first booking because of
this month's traffic. That property now lives in whoever computes the bounds, and it is a property
of the *caller* rather than of these rules; ``app.rules_stub`` resolves every window from the
booking's own local date for exactly this reason. The practical consequence is unchanged: history
reaches only as far as ``interfaces.history_window`` permits, so a request beyond it is measured
against a history the caller has no bookings for, sweeps into a run of its own, and passes if that
is not itself over the cap. That is the documented bound of the engine's promise — evaluation costs
at most one calendar month of history — not a gap in these rules.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .canon import _format_duration
from .interfaces import (
    BaseRule,
    BookingRecord,
    BookingRequest,
    Context,
    RuleResult,
    _require_utc,
)
from .spans import MergedSpan, merge_adjoining_spans

__all__ = [
    "MaxBookingsPerDayRule",
    "MaxBookingsPerWeekRule",
    "MaxBookingsPerMonthRule",
    "MaxDurationPerDayRule",
]


def _format_sessions(count: int) -> str:
    """Render a session count the way a person would say it, e.g. "1 session", "3 sessions".

    Named for what is counted since task 8.6: a booking two rows compose is one session, and
    telling a user they have "2 bookings" when they hold four rows that make two sessions is a
    message they cannot reconcile with their own calendar.
    """
    return f"{count} session" if count == 1 else f"{count} sessions"


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


def _count_runs_starting_within(runs: list[MergedSpan], lower: datetime, upper: datetime) -> int:
    """Count merged runs whose ``start_at`` lies in the half-open interval ``[lower, upper)``.

    A run belongs to the window it **starts** in, and to exactly one window: an interval that
    straddles midnight on a boundary would otherwise be counted twice, once against each side. This
    is the one piece of the old row-counting logic that carries over unchanged — what changed with
    task 8.6 is only what it counts (runs, already including the request) rather than raw rows.
    """
    return sum(1 for run in runs if lower <= run.start_at < upper)


def _sessions(
    request: BookingRequest, history: tuple[BookingRecord, ...], tolerance: timedelta
) -> list[MergedSpan]:
    """``request``'s own span folded into ``context.history.bookings`` and swept once.

    The request is always one of the spans the sweep merges — see the module docstring for why
    that happens here, at evaluate time, rather than ahead of time on ``Context.history`` itself.
    """
    spans = [(request.start_at, request.end_at)] + [
        (booking.start_at, booking.end_at) for booking in history
    ]
    return merge_adjoining_spans(spans, tolerance)


class MaxBookingsPerDayRule(BaseRule):
    """A user may hold at most ``max_bookings`` sessions in the local day the request falls in.

    Built exactly like its week and month siblings below — handed its window rather than deriving
    it, counting sessions (task 8.6's meaning) rather than raw rows. The day is
    ``[window_start, window_end)``, a pair of UTC instants the caller resolves from the venue's own
    local midnight (``app.rules_stub._local_day_bounds``) — the identical reasoning
    ``MaxBookingsPerWeekRule`` gives for its own window applies here without change: a Sydney
    booking at 00:30 local is the *previous* day in UTC, so a window snapped to UTC midnight would
    put it in the wrong local day and let a cap of one admit a second booking on the same Sydney
    day.

    ``tolerance`` is the gap ``evaluate`` closes when merging ``request`` with
    ``context.history.bookings`` into sessions — see the module docstring. It defaults to
    ``timedelta(0)`` (exact abutment only), matching every other counting rule here.

    The bound is compared directly against the merged session count, no ``+1`` — see the module
    docstring for why: the request is always one of the spans merged, so the count already accounts
    for it.

    The boundary is half-open. A session one second before local midnight belongs to the previous
    day and does not count; one starting exactly at local midnight belongs to this day and does.
    """

    def __init__(
        self,
        max_bookings: int,
        window_start: datetime,
        window_end: datetime,
        tolerance: timedelta = timedelta(0),
    ) -> None:
        if max_bookings <= 0:
            raise ValueError(
                f"MaxBookingsPerDayRule.max_bookings must be positive; got {max_bookings!r}"
            )
        self.max_bookings = max_bookings
        self.window_start, self.window_end = _validate_window(
            window_start, window_end, "MaxBookingsPerDayRule"
        )
        self.tolerance = tolerance

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        lower, upper = self.window_start, self.window_end
        runs = _sessions(request, context.history.bookings, self.tolerance)
        count = _count_runs_starting_within(runs, lower, upper)
        if count > self.max_bookings:
            return RuleResult.deny(
                f"You can make at most {_format_sessions(self.max_bookings)} a day,"
                f" and this booking would bring you to {_format_sessions(count)} that day."
                " Please pick another day."
            )
        return RuleResult.allow()


class MaxBookingsPerWeekRule(BaseRule):
    """A user may hold at most ``max_bookings`` sessions in the week the request falls in.

    The week is ``[window_start, window_end)``, a pair of UTC instants supplied by the caller. It is
    *not* derived here: which instants bound "the week this booking is in" depends on the venue's
    timezone, and the engine has none — see the module docstring for the miscount that followed from
    deriving it. The caller is expected to resolve the week containing the **request**, in the
    venue's own zone, honouring whatever first-day-of-week convention applies.

    ``tolerance`` is the gap ``evaluate`` closes when merging ``request`` with
    ``context.history.bookings`` into sessions — see the module docstring. It defaults to
    ``timedelta(0)`` (exact abutment only), which is what a caller resolving no ``min_duration`` for
    the date should pass, and what a test constructing this rule directly is usually happy to take.

    The bound is compared directly against the merged session count, no ``+1`` — see the module
    docstring for why: the request is always one of the spans merged, so the count already accounts
    for it.

    The boundary is half-open. A session one second before the window starts belongs to the previous
    week and does not count; one starting exactly at the boundary instant belongs to this week and
    does.
    """

    def __init__(
        self,
        max_bookings: int,
        window_start: datetime,
        window_end: datetime,
        tolerance: timedelta = timedelta(0),
    ) -> None:
        if max_bookings <= 0:
            raise ValueError(
                f"MaxBookingsPerWeekRule.max_bookings must be positive; got {max_bookings!r}"
            )
        self.max_bookings = max_bookings
        self.window_start, self.window_end = _validate_window(
            window_start, window_end, "MaxBookingsPerWeekRule"
        )
        self.tolerance = tolerance

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        lower, upper = self.window_start, self.window_end
        runs = _sessions(request, context.history.bookings, self.tolerance)
        count = _count_runs_starting_within(runs, lower, upper)
        if count > self.max_bookings:
            return RuleResult.deny(
                f"You can make at most {_format_sessions(self.max_bookings)} a week,"
                f" and this booking would bring you to {_format_sessions(count)} that week."
                " Please pick a time in another week."
            )
        return RuleResult.allow()


class MaxBookingsPerMonthRule(BaseRule):
    """A user may hold at most ``max_bookings`` sessions in the calendar month the request falls in.

    Calendar months, not rolling 30-day windows: the limit a user is told about is the one they can
    count on a calendar. December rolls into January like any other boundary — a December session
    does not count toward January's allowance.

    As with the weekly rule the month is ``[window_start, window_end)``, supplied by the caller
    rather than derived, for the same reason: "the calendar month this booking is in" is a question
    about the venue's local calendar, and the last local evening of a month is already the next
    month in UTC for any venue far enough ahead of it. ``tolerance`` is the merge gap — see
    ``MaxBookingsPerWeekRule`` and the module docstring.

    The bound is compared directly against the merged session count, no ``+1`` — see the module
    docstring. The window is half-open, so a session at the opening instant belongs to that month
    and one a second earlier does not.
    """

    def __init__(
        self,
        max_bookings: int,
        window_start: datetime,
        window_end: datetime,
        tolerance: timedelta = timedelta(0),
    ) -> None:
        if max_bookings <= 0:
            raise ValueError(
                f"MaxBookingsPerMonthRule.max_bookings must be positive; got {max_bookings!r}"
            )
        self.max_bookings = max_bookings
        self.window_start, self.window_end = _validate_window(
            window_start, window_end, "MaxBookingsPerMonthRule"
        )
        self.tolerance = tolerance

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        lower, upper = self.window_start, self.window_end
        runs = _sessions(request, context.history.bookings, self.tolerance)
        count = _count_runs_starting_within(runs, lower, upper)
        if count > self.max_bookings:
            return RuleResult.deny(
                f"You can make at most {_format_sessions(self.max_bookings)} a month,"
                f" and this booking would bring you to {_format_sessions(count)} that month."
                " Please pick a time in another month."
            )
        return RuleResult.allow()


def _sum_duration_within(
    history: tuple[BookingRecord, ...], lower: datetime, upper: datetime
) -> timedelta:
    """Sum the duration of every ``history`` entry whose ``start_at`` lies in ``[lower, upper)``.

    Deliberately **not** :func:`rules.spans.merge_adjoining_spans` — see ``MaxDurationPerDayRule``'s
    own docstring for why this rule sums raw entries instead of the runs every other rule in this
    module counts.
    """
    return sum(
        (
            booking.end_at - booking.start_at
            for booking in history
            if lower <= booking.start_at < upper
        ),
        timedelta(0),
    )


class MaxDurationPerDayRule(BaseRule):
    """A user may hold at most ``max_duration`` of total booked time in the local day the request
    falls in.

    The other shape a per-day limit takes, next to ``MaxBookingsPerDayRule`` above: a **total**, not
    a count. It sums the duration of every ``context.history.bookings`` entry that **starts** within
    ``[window_start, window_end)`` — the local day, resolved by the caller exactly as
    ``MaxBookingsPerDayRule``'s own window is — plus the request's own duration, and denies when
    that total exceeds ``max_duration``. The bound is inclusive: a day totalling exactly
    ``max_duration`` passes.

    **This rule sums raw entries. It deliberately does not call
    ``rules.spans.merge_adjoining_spans``, unlike every counting rule above it in this module — the
    one place in this stream runs are deliberately not used.** A total is the same number however
    the day's bookings are grouped: two one-hour bookings held back to back sum to two hours whether
    they are counted as one merged two-hour run or as two separate one-hour entries, so merging
    would spend the cost of a sweep to compute a number the raw entries already give for free. It is
    not merely unneeded, it is actively wrong in one case a run-based total would get silently
    wrong: a user holding **two Resources at overlapping times** — 17:00-18:00 on court 1 and
    17:30-18:30 on court 2. A run merges that into a single 17:00-18:30 span (``RunContext``'s own
    cross-Resource overlap handling), 90 minutes, when the user has actually booked two hours of
    court time across the two courts. Summing the raw entries instead gives the correct two hours;
    merging first would silently under-count by exactly the overlap. So this rule reads the raw
    entries it needs and sums them directly, never folding them into runs first.

    (This question was open when task 8.7 was planned and is now settled: task 8.6 kept
    ``Context.history`` as raw, unmerged rows — it had to, since folding the request into that
    field breaks ``Context.__post_init__``'s history-window invariant for a request beyond the
    window, the same conflict the module docstring describes for the counting rules. The merging
    those rules do happens inside their own ``evaluate``, not on ``Context.history`` itself, so
    there was never a pre-merged history for this rule to read even if it wanted one.)

    ``window_start``/``window_end`` are validated exactly as every other rule in this module
    validates its own window — a half-open UTC pair, inverted or naive rejected as a caller bug.
    There is no ``tolerance`` parameter: nothing here merges, so there is no gap to close.
    """

    def __init__(
        self,
        max_duration: timedelta,
        window_start: datetime,
        window_end: datetime,
    ) -> None:
        if max_duration <= timedelta(0):
            raise ValueError(
                f"MaxDurationPerDayRule.max_duration must be positive; got {max_duration!r}"
            )
        self.max_duration = max_duration
        self.window_start, self.window_end = _validate_window(
            window_start, window_end, "MaxDurationPerDayRule"
        )

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        lower, upper = self.window_start, self.window_end
        total = request.duration + _sum_duration_within(context.history.bookings, lower, upper)
        if total > self.max_duration:
            return RuleResult.deny(
                f"Bookings can add up to at most {_format_duration(self.max_duration)} a day,"
                f" and this one would bring your total to {_format_duration(total)} today."
                " Please shorten it, or pick another day, and try again."
            )
        return RuleResult.allow()


# Deliberately absent from ``DEFAULT_CANON``. The four rules in ``canon.py`` are what Stream 1's
# end-to-end suite asserts against, and adding a booking limit to the default canon would change
# behaviour those tests depend on at integration. The four rules in this module are exported for a
# caller that wants them; wiring them into a canon is a later task's decision, alongside per-Space
# configuration.
