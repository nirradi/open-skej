"""Boundary conversion from a Space's local operating hours to a UTC window.

This is the one place a Space's IANA ``timezone`` and its own local
``opens_at`` / ``closes_at`` meet a calendar date and produce the UTC clock
times the rule engine actually understands. See ``.claude/rules/identity-
and-access.md`` ("Timezone lives on the Space") for why the zone lives where
it does, and the "Instants carry no zone; recurring wall-clock config carries
an IANA name" / "Conversion happens at the boundary" rows of
``ops/plans/stream-4-plan.md`` for why this module exists at all rather than
folding the conversion into the engine or the ORM.

``rules.canon.AvailabilityHoursRule`` takes ``opens_at`` / ``closes_at`` as
**UTC** clock times and never converts anything itself — that is deliberate,
not an oversight this module patches over. A Space's operating hours are
authored and stored as *local* wall clock (a venue that opens at 07:00 does
so in its own morning, not in Greenwich's), so something has to resolve
"07:00, Europe/Berlin" to a UTC instant before the engine ever sees it. This
module is that something, and it is deliberately the *only* thing it is: no
ORM import, no engine import, a pure function of its four arguments so it is
trivial to unit test without a database and safe for task 4.13b to call from
the ``rules_stub`` boundary without dragging either dependency along.

**Why the conversion cannot happen once, at write time.** A fixed UTC offset
stored alongside the hours would be correct on the day it was computed and
silently wrong every time the zone's DST rule flips — an offset column is
the version of this that looks right in July and is wrong in January. The
conversion must therefore be repeated **per date**, at the boundary, on
every call: ``on_date`` is not a formality, it is the reason this function
takes a date at all instead of just a zone name.

**A window may resolve to two different UTC calendar dates.** A perfectly
ordinary same-local-day window — Sydney 09:00-21:00, Honolulu 08:00-20:00 —
lands its opening instant on the UTC calendar date *before* ``on_date``
whenever the zone's offset is large enough (Sydney is UTC+10/+11; anything
open before roughly its own offset hits this, not just the UTC+13/+14 zones
a narrower reading of "midnight wrap" might suggest). Because the return
value here is a pair of bare ``time`` values with no date attached, that
case comes back as ``utc_open > utc_close`` — an "inverted" pair by string
comparison, but not an error: it is exactly how "opens on the UTC date
before ``on_date``, closes on ``on_date`` itself" has to look once the date
is dropped. ``rules.canon.AvailabilityHoursRule`` is what reads that
inversion correctly, reconstructing which UTC calendar date each bound
belongs to from the booking it is judging rather than from ``on_date`` here,
which by the time a rule runs is long gone. This function used to refuse to
return such a pair at all (``MidnightWrapError``); doing so made an entirely
ordinary Sydney or Honolulu venue's hours unrepresentable and every booking
against it fail — see ``DEFERRED.md`` items 16 and 17. It no longer refuses.
"""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

__all__ = ["resolve_operating_hours"]


def resolve_operating_hours(
    opens_at: time, closes_at: time, tz_name: str, on_date: date
) -> tuple[time, time]:
    """Resolve a Space's local operating hours to UTC clock times for ``on_date``.

    ``opens_at`` / ``closes_at`` are the Space's stored local wall-clock hours;
    ``tz_name`` is that same Space's IANA zone (``Europe/Berlin``, never a
    fixed offset); ``on_date`` is the calendar date — in that local zone — the
    hours apply to. The return value is the equivalent UTC clock times for
    that same date, which is exactly the shape
    ``rules.canon.AvailabilityHoursRule`` is constructed with — including,
    per the module docstring, the case where the pair comes back inverted
    (``utc_open > utc_close``) because the local window's opening instant
    falls on the UTC calendar date before ``on_date``.

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
    * **The UTC-day-crossing case** — the local window resolves to an
      *inverted* pair, ``utc_open > utc_close``. This is not an error: it is
      the only way "opens on the UTC date before ``on_date``, closes on
      ``on_date``" can be expressed once the date is dropped from the return
      value. See the module docstring and ``rules.canon.AvailabilityHoursRule``,
      which is what turns the inversion back into two correctly-dated
      instants when it judges one specific booking.
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
    ``opens_at`` / ``closes_at`` pair that is ordered as a *local* same-day
    window (``opens_at < closes_at``, the Space's own wall clock — a venue
    open past its own local midnight is a distinct, unsupported
    configuration, ``DEFERRED.md`` item 18, and not this function's concern).
    Their UTC-converted images may or may not preserve that order: a zone
    close enough to UTC keeps ``utc_open < utc_close`` (Berlin), while one
    far enough ahead of UTC — Sydney included, not only the UTC+13/+14 zones
    a narrower reading might suggest — resolves to ``utc_open > utc_close``.
    Both are valid returns; see the module docstring.

    ``slot_minutes`` needs no equivalent function: it is a duration, not a
    clock time, and a duration is the same length of time in every zone.
    """
    tz = ZoneInfo(tz_name)
    utc_open = datetime.combine(on_date, opens_at, tzinfo=tz).astimezone(timezone.utc).time()
    utc_close = datetime.combine(on_date, closes_at, tzinfo=tz).astimezone(timezone.utc).time()
    return utc_open, utc_close
