"""Boundary conversion from a Resource's local operating hours to a UTC window.

This is the one place the Space's IANA ``timezone`` and a Resource's local
``opens_at`` / ``closes_at`` meet a calendar date and produce the UTC clock
times the rule engine actually understands. See ``.claude/rules/identity-
and-access.md`` ("Timezone lives on the Space") for why the zone lives where
it does, and the "Instants carry no zone; recurring wall-clock config carries
an IANA name" / "Conversion happens at the boundary" rows of
``ops/plans/stream-4-plan.md`` for why this module exists at all rather than
folding the conversion into the engine or the ORM.

``rules.canon.AvailabilityHoursRule`` takes ``opens_at`` / ``closes_at`` as
**UTC** clock times and never converts anything itself — that is deliberate,
not an oversight this module patches over. A Resource's operating hours are
authored and stored as *local* wall clock (a court that opens at 07:00 does
so in the venue's own morning, not in Greenwich's), so something has to
resolve "07:00, Europe/Berlin" to a UTC instant before the engine ever sees
it. This module is that something, and it is deliberately the *only* thing
it is: no ORM import, no engine import, a pure function of its four
arguments so it is trivial to unit test without a database and safe for
task 4.13 to call from the ``rules_stub`` boundary without dragging either
dependency along.

**Why the conversion cannot happen once, at write time.** A fixed UTC offset
stored alongside the hours would be correct on the day it was computed and
silently wrong every time the zone's DST rule flips — an offset column is
the version of this that looks right in July and is wrong in January. The
conversion must therefore be repeated **per date**, at the boundary, on
every call: ``on_date`` is not a formality, it is the reason this function
takes a date at all instead of just a zone name.
"""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

__all__ = ["resolve_operating_hours", "MidnightWrapError"]


class MidnightWrapError(ValueError):
    """A local operating window resolved to a UTC window that wraps past midnight.

    Raised by :func:`resolve_operating_hours` when converting ``opens_at`` /
    ``closes_at`` to UTC yields ``utc_open >= utc_close`` — the pair can no
    longer be read as "open from X to Y later the same UTC day".

    This is a real edge, not a hypothetical: a zone far enough from UTC (e.g.
    ``Pacific/Auckland``, UTC+13) shifts an ordinary local window like
    06:00-23:00 back across the UTC calendar-day boundary, so the *opening*
    time lands on the *previous* UTC date while the closing time does not.
    Dropped to bare ``time`` values — which is what
    ``rules.canon.AvailabilityHoursRule`` accepts and what this function
    therefore must return — that distinction is lost, and returning the pair
    anyway would hand the engine an availability window that is silently
    inverted rather than one that spans two UTC dates.

    The safe default is to refuse rather than guess: raising here is what
    keeps a broken window from reaching the engine as a pair of `time`
    values that happen to compare the wrong way. A Space whose venue and
    configured hours combine to trigger this is real (any zone at UTC+13/+14,
    or a window wide enough relative to a zone's offset) and is a
    configuration problem for a human to resolve — split the window, or pick
    hours that do not wrap — not one this function can silently paper over by
    returning a value that would misbehave downstream.
    """


def resolve_operating_hours(
    opens_at: time, closes_at: time, tz_name: str, on_date: date
) -> tuple[time, time]:
    """Resolve a Resource's local operating hours to UTC clock times for ``on_date``.

    ``opens_at`` / ``closes_at`` are the Resource's stored local wall-clock
    hours; ``tz_name`` is the parent Space's IANA zone (``Europe/Berlin``,
    never a fixed offset); ``on_date`` is the calendar date — in that local
    zone — the hours apply to. The return value is the equivalent UTC clock
    times for that same date, which is exactly the shape
    ``rules.canon.AvailabilityHoursRule`` is constructed with.

    **DST is the point, not an edge case.** The same wall-clock input
    resolves to a *different* UTC time depending on ``on_date`` — 07:00
    Europe/Berlin is 05:00Z in July (CEST, UTC+2) and 06:00Z in January (CET,
    UTC+1). Freezing a single UTC offset at configuration time would open
    the venue an hour early (or late) for half the year; that is precisely
    the bug this function exists to prevent, so the conversion is repeated
    for every ``on_date`` rather than cached or computed once.

    **Hazards, handled explicitly:**

    * **Unknown or invalid ``tz_name``** fails loudly. ``ZoneInfo(tz_name)``
      raises ``zoneinfo.ZoneInfoNotFoundError`` (a ``KeyError`` subclass) for
      a name the system's tzdata does not recognise, and that exception is
      left to propagate rather than caught and papered over with a UTC
      fallback — a silent fallback here would open or close a venue at the
      wrong instant with no visible error. Validating a zone name a human
      *typed* is task 4.12's job (rejecting it before it is ever stored);
      this function's only obligation is to never silently substitute a
      different zone for a bad one it is handed.
    * **The midnight-wrap case** — see :class:`MidnightWrapError`. Detected
      by comparing the two resolved UTC times and raised rather than
      returned, because a caller that only sees two ``time`` values has no
      way to tell "inverted by wraparound" from "legitimately open all but
      one hour."
    * **DST gap and fold instants** — a local time that does not exist (the
      hour skipped in a spring-forward transition) or exists twice (the hour
      repeated in a fall-back one) — are resolved by ``zoneinfo``'s own
      documented behaviour (PEP 495): a nonexistent local time is
      extrapolated from the offset either side of the gap, and an ambiguous
      one resolves to its earlier (``fold=0``) offset by default. Both are
      accepted as-is rather than special-cased. Operating hours are
      configured once and evaluated against many dates; a rule precise
      enough to special-case the handful of hours a year a zone transitions
      would buy correctness nobody asked for at the cost of a second code
      path nobody can verify by inspection.

    **Supported range.** Any IANA zone name ``ZoneInfo`` accepts, for any
    ``opens_at`` / ``closes_at`` pair whose UTC-converted images preserve
    their order (``utc_open < utc_close``). That covers ordinary daytime
    operating hours in every zone from roughly UTC-11 to UTC+12; a window
    that wraps past midnight in UTC (``Pacific/Auckland`` and other
    UTC+13/+14 zones are the practical case) raises
    :class:`MidnightWrapError` instead of returning an inverted pair.

    ``slot_minutes`` needs no equivalent function: it is a duration, not a
    clock time, and a duration is the same length of time in every zone.
    """
    tz = ZoneInfo(tz_name)
    utc_open = datetime.combine(on_date, opens_at, tzinfo=tz).astimezone(timezone.utc).time()
    utc_close = datetime.combine(on_date, closes_at, tzinfo=tz).astimezone(timezone.utc).time()

    if utc_open >= utc_close:
        raise MidnightWrapError(
            f"resolving {opens_at}-{closes_at} local ({tz_name}) on {on_date} yields"
            f" a UTC window of {utc_open}-{utc_close}, which wraps past midnight and"
            f" cannot be expressed as a single same-day (open, close) pair"
        )

    return utc_open, utc_close
