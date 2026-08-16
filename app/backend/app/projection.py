"""The server-side calendar projection: what a rendered window is actually bookable for.

Today the calendar grid is drawn from two rule types the frontend knows by name
(``availability_hours`` and ``session_length``) — a Space configuring anything else, a
``max_duration``, a ``max_bookings_per_week``, or an AI-generated rule that closes a Tuesday
afternoon, is invisible to the grid: the calendar keeps offering slots the server then refuses.
This module is the alternative the calendar *could* draw from instead: for every candidate slot
start in a rendered window, ask the real canon — the exact rules ``rules.evaluate_request`` would
run against a live booking, in the exact order — how long a booking starting there is allowed to
be. Nothing here is a second, faster implementation of any rule's semantics; every verdict is a
real ``evaluate_request`` call, because a projection that disagreed with the booking endpoint would
be worse than the two-rule-type grid it replaces — at least that one fails obviously.

**Why this module knows no database and no timezone.** ``rules/rules/interfaces.py`` states the
two invariants that make projecting a whole window affordable to even attempt: history arrives
pre-filtered (``HistoryContext``) and every local question — the venue's day, its opening time in
minutes, a request's own local start — arrives pre-answered (``LocalFrame``). A rule never queries
a database and never converts a timezone; it only ever judges a ``(BookingRequest, Context)`` pair
someone else assembled. This module extends that discipline one layer up rather than breaking it:
``make_request_and_context`` is the caller's own context-builder — in production the same
machinery ``app/backend/app/rules_stub.py`` uses for one real booking (``_build_local_frame``,
``_resolve_run``, ``_engine_request``), reused here rather than re-derived — and ``project_days``
never touches a Space, a timezone name, or a booking's storage. It hands that builder two plain
Python ``datetime`` values per candidate and trusts it completely for everything local.

**Those two datetimes are the venue's own local wall clock, not UTC.** ``DayGrid`` describes a
day's rendered grid — a calendar ``date`` and minutes from **that date's local midnight** — because
that is the only vocabulary a grid is drawn in; a UTC instant would need a timezone to turn back
into "9am" for a human, and this module holds none. ``project_days`` builds each candidate instant
as ``datetime.combine(day.date, time.min) + timedelta(minutes=...)`` — a naive datetime whose wall
clock reads the venue's own local time — and hands it to ``make_request_and_context`` exactly as
naive. That callable is where the real timezone conversion happens (mirroring
``rules_stub._to_utc`` / ``_build_local_frame``), the same way the adapter is the only thing in the
whole system that is allowed to know one (``.claude/rules/rule-engine.md``). A caller that supplies
a context-builder which does *not* convert has built something dishonest, not something this module
can detect — the same trust ``evaluate_request`` already places in whatever context it is handed.

**The algorithm is brute-force scanning, deliberately.** For a slot starting at ``s``, this asks
"is 1 slot bookable? 2? 3? …" by literally building that candidate and calling
``rules.evaluate_request`` — it does not special-case ``MaxDurationRule`` to compute the cutoff
arithmetically, or ``SessionLengthRule`` to jump straight to the next grid-aligned length, even
though both would be cheap and correct today. Doing that would silently stop being correct the
first time a Space configures a rule type this module was never taught the shortcut for — an
AI-generated rule closing Thursday afternoons has no formula to special-case against. Brute force
is also the entire question this POC exists to answer: whether evaluating the real canon this many
times, this literally, is affordable for a rendered window. If it is not, the fix is not a smarter
scan here, it is a smaller `max_scan_slots` or a coarser grid.

**What "allowed lengths" means, and why min and max are not "1..n".** A rule can allow 60 minutes
and refuse 30 — ``SessionLengthRule`` with a 60-minute session anchored on opening time allows
exactly 60, 120, 180 … and denies every length in between. So ``min_slots`` is the smallest allowed
length, but ``max_slots`` is not "the largest allowed length found" — it is the top of the
**contiguous run of allowed lengths starting at ``min_slots``**, which is what a calendar's
"drag to extend" UI actually needs: how far this specific booking can stretch before hitting a
wall, not whether some longer, disconnected length happens to also be legal. A length allowed again
after that wall (a real possibility for an adversarial or AI-generated rule) is invisible to
``max_slots`` by design.

**``early_stop`` trades a real cost for a real answer, and the benchmark exists to price the
trade.** ``early_stop=True`` stops scanning a slot the moment a denial follows an allow — the
projection is already fully determined at that point, so every further ``evaluate_request`` call
would be spent computing nothing new. ``early_stop=False`` scans every candidate length up to the
day boundary (or ``max_scan_slots``) regardless, producing an identical ``SlotProjection`` at
several times the call count. Neither path shortcuts a slot where **nothing** is ever allowed —
there is no "first denial following an allow" to stop at, so a fully-closed window is scanned to
its natural end under both settings. That is not an oversight: it is the worst case this module
has, and the benchmark reports it as one rather than hiding it behind a clever early exit that
would not exist for a real, unpredictable canon.

**What this module deliberately does not do.** It does not cache an ``evaluate_request`` verdict
across slot starts, even though two different starts can ask about an overlapping instant — a POC
measuring the naive cost should not be flattered by an optimization a production version might
never actually need (most of the cost, per the benchmark, is duration-cap-shaped and does not
repeat across starts the way a cache would help with). It does not know what a "denying rule" is —
``RuleResult`` carries only user-facing ``fail_reason`` text (``rules/rules/interfaces.py``), never
a rule identity, so ``reason_code`` is derived from that text alone; see
``_derive_reason_code`` for exactly how POC-grade that derivation is.

**``project_days`` itself never sees a Resource's own bookings.** ``canon_for_day`` runs the
Space's real rules and nothing else — no synthetic "already booked" rule belongs in it. Whether a
candidate overlaps an existing booking is applied afterward, by ``clip_overlap``, against the
already-computed result: interval arithmetic over one Resource's own bookings, never a further
``evaluate_request`` call. That split is what makes a ``project_days`` result depend only on the
Space and the date — never on which Resource or which member is asking — which is the property
``app.projection_cache`` caches against; see that module's and ``clip_overlap``'s own docstrings.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from rules import BaseRule, BookingRequest, Context, evaluate_request

__all__ = [
    "SlotProjection",
    "DayGrid",
    "DayProjection",
    "project_days",
    "clip_overlap",
]

#: A local calendar day is 1440 minutes. ``DayGrid`` carries no day-length field of its own — a POC
#: simplification, called out here rather than silently assumed: a real Space's local day is 23 or
#: 25 minutes-per-hour-times-shorter on the two DST-transition dates a year
#: (``app/backend/app/rules_stub.py``'s own ``_local_day_bounds``), and this module has no timezone
#: with which to know which dates those are for a given Space. A production caller projecting a DST
#: boundary date would need this made a per-``DayGrid`` field; nothing here notices the gap on its
#: own, it just always scans against a 1440-minute day.
_MINUTES_PER_LOCAL_DAY = 1440


@dataclass(frozen=True)
class SlotProjection:
    """What a booking starting at ``start_minutes`` (this day's local midnight) may be.

    ``min_slots`` / ``max_slots`` are counted in **grid slots**, not minutes — multiply by the
    owning ``DayProjection.slot_minutes`` to get a duration. Both are ``0`` exactly when nothing
    may be booked from this start, which is the only condition under which ``reason_code`` /
    ``reason_text`` are populated; see the module docstring for why ``max_slots`` is the top of the
    *contiguous* run of allowed lengths rather than the largest allowed length found anywhere.
    """

    start_minutes: int
    min_slots: int
    max_slots: int
    reason_code: str | None = None
    reason_text: str | None = None

    def __post_init__(self) -> None:
        if self.start_minutes < 0:
            raise ValueError(
                f"SlotProjection.start_minutes must be non-negative; got {self.start_minutes!r}"
            )
        if self.min_slots < 0 or self.max_slots < 0:
            raise ValueError(
                f"SlotProjection.min_slots/max_slots must be non-negative; got "
                f"min_slots={self.min_slots!r}, max_slots={self.max_slots!r}"
            )
        if self.max_slots < self.min_slots:
            raise ValueError(
                f"SlotProjection.max_slots must be >= min_slots; got "
                f"min_slots={self.min_slots!r}, max_slots={self.max_slots!r}"
            )
        if self.min_slots == 0:
            if self.max_slots != 0:
                raise ValueError("SlotProjection with min_slots=0 must also have max_slots=0")
            if self.reason_text is None:
                raise ValueError("SlotProjection with min_slots=0 must carry a reason_text")
        else:
            if self.reason_code is not None or self.reason_text is not None:
                raise ValueError(
                    "SlotProjection with min_slots > 0 must not carry a reason_code/reason_text — "
                    "it was not denied"
                )


@dataclass(frozen=True)
class DayGrid:
    """One day's rendered grid, in the venue's own local wall-clock terms.

    ``date`` plus minutes from *that date's* local midnight — never a UTC instant, and never a
    timezone name, because this module holds neither (module docstring). ``first_slot_minutes`` is
    where slot ``0`` starts; the grid then advances by ``slot_minutes`` for ``slot_count`` slots. A
    caller ordinarily derives all three from the identical resolution ``resolve_day_schedule``
    already performs for the calendar UI (``app/backend/app/rules_stub.py``), so the window this
    projects is the window the grid would actually draw — not a different, wider guess at one.
    """

    date: date
    slot_minutes: int
    first_slot_minutes: int
    slot_count: int

    def __post_init__(self) -> None:
        if self.slot_minutes <= 0:
            raise ValueError(f"DayGrid.slot_minutes must be positive; got {self.slot_minutes!r}")
        if self.first_slot_minutes < 0:
            raise ValueError(
                f"DayGrid.first_slot_minutes must be non-negative; got {self.first_slot_minutes!r}"
            )
        if self.slot_count < 0:
            raise ValueError(f"DayGrid.slot_count must be non-negative; got {self.slot_count!r}")


@dataclass(frozen=True)
class DayProjection:
    """The projected result for one ``DayGrid`` — one ``SlotProjection`` per rendered slot start,
    in the same order ``slot_count`` iterates them."""

    date: date
    slot_minutes: int
    first_slot_minutes: int
    slots: tuple[SlotProjection, ...]


def _derive_reason_code(fail_reason: str) -> str:
    """A short, stable identifier for a denial, derived from its own user-facing copy.

    **POC-grade, stated plainly.** The engine deliberately never tells a caller *which rule*
    denied — ``RuleResult`` carries only ``fail_reason``, friendly text meant for a user, never a
    rule name (``rules/rules/interfaces.py``) — so the denial's own copy is the only signal this
    function has to work with. It normalizes whitespace/case and hashes the result rather than
    pattern-matching known phrases: pattern-matching would need a new case every time a rule's copy
    changes a word, and would silently produce nothing at all for a generated rule's message it was
    never taught about. Hashing costs nothing to keep current, at the price of a code that is not
    human-readable and that changes if the copy's wording ever does — acceptable for grouping
    identical denials inside a demo or a benchmark, not a fit for anything that needs a code stable
    across a copy edit (a support macro, an analytics dashboard) without a real rule-identity field
    to key on instead.
    """
    normalized = " ".join(fail_reason.strip().lower().split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"deny_{digest}"


def _scan_slot(
    *,
    day_start: datetime,
    start_minutes: int,
    slot_minutes: int,
    canon: Sequence[BaseRule],
    make_request_and_context: Callable[[datetime, datetime], tuple[BookingRequest, Context]],
    max_scan_slots: int | None,
    early_stop: bool,
    call_counter: list[int] | None,
) -> SlotProjection:
    """Scan candidate lengths ``k = 1, 2, 3, …`` from one slot start. See the module docstring for
    the full algorithm; this is the per-start inner loop ``project_days`` runs once per grid slot.
    """
    min_slots: int | None = None
    max_slots: int | None = None
    run_broken = False
    first_denial_text: str | None = None

    k = 1
    while True:
        end_minutes = start_minutes + k * slot_minutes
        if end_minutes > _MINUTES_PER_LOCAL_DAY:
            break
        if max_scan_slots is not None and k > max_scan_slots:
            break

        start_at = day_start + timedelta(minutes=start_minutes)
        end_at = day_start + timedelta(minutes=end_minutes)
        request, context = make_request_and_context(start_at, end_at)
        result = evaluate_request(request, context, canon)
        if call_counter is not None:
            call_counter[0] += 1

        if result.passed:
            if min_slots is None:
                min_slots = k
                max_slots = k
            elif not run_broken:
                max_slots = k
            # A pass after the run already broke is a length allowed again past the wall — not
            # part of the contiguous run `max_slots` reports (module docstring).
        else:
            if k == 1:
                first_denial_text = result.fail_reason
            if min_slots is not None and not run_broken:
                run_broken = True
                if early_stop:
                    break
            # A denial before any allow, or after the run already broke, changes nothing further.

        k += 1

    if min_slots is None:
        assert first_denial_text is not None, (
            "k=1 is always scanned first (a slot's start is always inside the local day), so a "
            "slot with nothing allowed always has a k=1 denial to carry as reason_text"
        )
        return SlotProjection(
            start_minutes=start_minutes,
            min_slots=0,
            max_slots=0,
            reason_code=_derive_reason_code(first_denial_text),
            reason_text=first_denial_text,
        )

    assert max_slots is not None
    return SlotProjection(start_minutes=start_minutes, min_slots=min_slots, max_slots=max_slots)


def project_days(
    *,
    canon_for_day: Callable[[date], Sequence[BaseRule]],
    make_request_and_context: Callable[[datetime, datetime], tuple[BookingRequest, Context]],
    days: Sequence[DayGrid],
    max_scan_slots: int | None = None,
    early_stop: bool = True,
    call_counter: list[int] | None = None,
) -> tuple[DayProjection, ...]:
    """Project every ``DayGrid`` in ``days``, one ``DayProjection`` each.

    **Why a callable and not one fixed ``canon`` for the whole window.** A Space's rules carry an
    ``applies_to`` scope of weekdays or specific dates (``app/backend/app/rules_stub.py``'s
    ``row_applies``), and ``session_length``'s ``anchor_minutes`` is resolved per date
    (``resolve_day_schedule``) — so two different days in the same window can legitimately run
    different canons, exactly the way ``rules_stub.evaluate()`` rebuilds its canon fresh for every
    single booking rather than caching one. A single ``canon`` argument would be correct only for a
    Space that happens not to use per-day scoping yet, and would silently go stale the first time
    one did. ``canon_for_day`` is called exactly once per ``DayGrid`` (not once per slot, and not
    once per scanned candidate length) — the same granularity ``_build_canon(config, on_date)``
    already resolves at, since nothing about a canon depends on a slot's start or a candidate's
    length, only on its date. The alternative shape considered was a ``canon`` field on ``DayGrid``
    itself, rejected because ``DayGrid`` describes a rendered grid — a UI-facing date/slot-geometry
    concern — and folding "which rules govern this date" into it would make every caller that only
    wants to describe a grid (a test building one by hand) also responsible for resolving a canon.
    A callable keeps the two concerns — the grid's shape, and how a canon is derived from a date —
    decoupled, and lets one callable close over a single ``SpaceRuleConfig`` for an entire window,
    mirroring ``make_request_and_context``'s own existing shape.

    Each resolved canon is evaluated exactly as ``rules.evaluate_request`` always evaluates it — in
    order, fail-fast — because this function calls nothing else; it is not a reimplementation of any
    rule's semantics (module docstring). ``make_request_and_context`` is called once per scanned
    candidate length, with two **naive local wall-clock** datetimes built from ``day.date`` and the
    candidate's own start/end minutes — never a UTC instant, since this function holds no timezone
    with which to produce one. It is the caller's job to convert, exactly as
    ``app/backend/app/rules_stub.py`` converts for one real booking.

    ``max_scan_slots`` bounds every scan at the same length regardless of ``day``, mainly useful to
    cap the worst case when no ``max_duration`` is configured (module docstring, and
    ``projection_bench.py``, which measures exactly that case). ``early_stop`` is documented on
    ``SlotProjection`` and in the module docstring; both settings visit **every** candidate length
    for a slot where nothing is ever allowed, which is the worst case this module has.

    ``call_counter``, if given, is incremented by one for every ``evaluate_request`` call made
    across every day and every slot — the total the benchmark reports call counts and per-call
    timing against. Pass ``[0]`` and read ``call_counter[0]`` back afterwards; ``None`` (the
    default) skips counting entirely.
    """
    projections: list[DayProjection] = []

    for day in days:
        day_start = datetime.combine(day.date, time.min)
        canon = canon_for_day(day.date)
        slots = tuple(
            _scan_slot(
                day_start=day_start,
                start_minutes=day.first_slot_minutes + index * day.slot_minutes,
                slot_minutes=day.slot_minutes,
                canon=canon,
                make_request_and_context=make_request_and_context,
                max_scan_slots=max_scan_slots,
                early_stop=early_stop,
                call_counter=call_counter,
            )
            for index in range(day.slot_count)
        )
        projections.append(
            DayProjection(
                date=day.date,
                slot_minutes=day.slot_minutes,
                first_slot_minutes=day.first_slot_minutes,
                slots=slots,
            )
        )

    return tuple(projections)


def _clip_slot(
    slot: SlotProjection,
    *,
    slot_minutes: int,
    intervals: Sequence[tuple[float, float]],
    reason_code: str,
    reason_text: str,
) -> SlotProjection:
    """Clip one already-allowed ``SlotProjection`` against ``intervals`` (see ``clip_overlap``).

    A denied slot (``min_slots == 0``) is returned unchanged — it was never bookable in the first
    place, so there is nothing an existing booking could add to that verdict, and this function
    must not overwrite a real rule's own ``reason_code``/``reason_text`` with an overlap one that
    did not actually decide anything here.
    """
    if slot.min_slots == 0:
        return slot

    start = slot.start_minutes

    for interval_start, interval_end in intervals:
        if interval_start <= start < interval_end:
            # The start itself is already inside a booking — nothing from here is bookable,
            # regardless of what the rules-only scan found.
            return SlotProjection(
                start_minutes=start,
                min_slots=0,
                max_slots=0,
                reason_code=reason_code,
                reason_text=reason_text,
            )

    # The nearest booking that starts *after* this slot's own start is the only one that can
    # shorten the contiguous run the rules-only scan already found — a booking further out can
    # never bind tighter than one already closer in (module docstring's own "top of the
    # contiguous run" reasoning, carried one step further here).
    upcoming_starts = [interval_start for interval_start, _ in intervals if interval_start > start]
    ceiling = min(upcoming_starts) if upcoming_starts else None

    rules_only_end = start + slot.max_slots * slot_minutes
    capped_end = rules_only_end if ceiling is None else min(rules_only_end, ceiling)
    clipped_max_slots = int((capped_end - start) // slot_minutes)

    if clipped_max_slots < slot.min_slots:
        # Even the shortest allowed length now collides with a booking — the whole start is
        # unbookable, not merely shortened.
        return SlotProjection(
            start_minutes=start,
            min_slots=0,
            max_slots=0,
            reason_code=reason_code,
            reason_text=reason_text,
        )

    if clipped_max_slots == slot.max_slots:
        return slot

    return SlotProjection(
        start_minutes=start, min_slots=slot.min_slots, max_slots=clipped_max_slots
    )


def clip_overlap(
    day: DayProjection,
    intervals: Sequence[tuple[float, float]],
    *,
    reason_code: str,
    reason_text: str,
) -> DayProjection:
    """Shorten ``day``'s already-allowed runs so none of them invite a booking that would overlap
    an existing one — by interval arithmetic alone, never a second ``evaluate_request`` call.

    **Why this is a separate step from the scan, and not another synthetic rule in the canon.**
    Whether a candidate overlaps an existing booking is a fact about one Resource's own calendar,
    not about the Space's rules — the same distinction ``app.db.driver.BookingDriver`` already
    draws between an integrity invariant and a configurable rule. Folding it into the canon (as a
    prior version of this endpoint's ``_ExistingBookingRule`` did) makes the projection depend on
    which Resource is asked about, which defeats caching the projection per Space and per date
    (``app.projection_cache``) — every Resource in a Space would need its own cache entry, keyed on
    a live table that changes on every booking made or cancelled. Clipping the already-computed,
    Resource-independent rules-only answer against one Resource's own bookings afterward keeps the
    cache Resource-agnostic while still never reporting an overlapping slot as bookable.

    ``intervals`` are the Resource's own bookings for the day, as ``(start_minutes, end_minutes)``
    pairs from *this day's own local midnight* — the identical vocabulary ``DayGrid`` and
    ``SlotProjection.start_minutes`` already use (module docstring), so the caller has done the one
    timezone conversion this function itself refuses to do. A booking spanning midnight contributes
    an interval whose bound extends past ``[0, 1440)``; ordinary comparison against a slot's own
    in-range ``start_minutes`` handles that correctly without this function needing to know the day
    has an edge.

    Only a slot the rules-only scan already allowed (``min_slots > 0``) is ever touched — a slot the
    canon itself denied keeps that denial's own ``reason_code``/``reason_text`` verbatim, so an
    admin-configured rule's own message is never silently replaced by "already booked" for a slot
    it was never going to offer anyway. A touched slot either keeps its own ``min_slots`` with a
    shorter ``max_slots`` (the run is still offerable, just not as far), or is zeroed out with
    ``reason_code``/``reason_text`` when even the shortest allowed length would now overlap.
    """
    clipped_slots = tuple(
        _clip_slot(
            slot,
            slot_minutes=day.slot_minutes,
            intervals=intervals,
            reason_code=reason_code,
            reason_text=reason_text,
        )
        for slot in day.slots
    )
    return DayProjection(
        date=day.date,
        slot_minutes=day.slot_minutes,
        first_slot_minutes=day.first_slot_minutes,
        slots=clipped_slots,
    )
