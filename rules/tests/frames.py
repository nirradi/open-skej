"""A ``LocalFrame`` for tests whose venue is on UTC.

``Context.local`` is required and has no default, deliberately (``interfaces.Context``): the only
frame a default could name is the UTC one, and a rule reading that would be silently wrong for every
venue off UTC. That decision costs every test in this package a frame, and this is where they get
one — a single honest resolver rather than a literal repeated in seven modules, any one of which
could drift into a frame that describes a different day than the request it is handed with.

It resolves for a **UTC venue**, which is what these tests are: nothing in ``rules/`` knows a
timezone, so a suite here has no zone to be in. The zone-dependent resolution is the adapter's and
is tested where it lives, in the backend's own suite against real IANA zones — including a DST
transition, which a UTC venue by construction cannot have.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

from rules import LocalFrame, Weekday

__all__ = ["utc_frame"]


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=timezone.utc)


def utc_frame(
    start_at: datetime,
    end_at: datetime | None = None,
    *,
    week_starts_on: Weekday = Weekday.MONDAY,
) -> LocalFrame:
    """The frame a UTC-hosted Space's adapter would resolve for a booking at ``start_at``.

    ``end_at`` defaults to an hour after the start, so a helper that has only an instant to hand
    still gets a coherent frame. Mirrors ``app.rules_stub._build_local_frame`` — every bound taken
    from a midnight, never from a fixed offset off another bound — because a frame assembled some
    other way here would let a rule pass against a shape production never produces.
    """
    if end_at is None:
        end_at = start_at + timedelta(hours=1)

    day = start_at.date()
    day_start = _midnight(day)

    first_weekday = day - timedelta(days=(day.weekday() - int(week_starts_on)) % 7)
    first_of_month = day.replace(day=1)
    next_month = (
        first_of_month.replace(year=day.year + 1, month=1)
        if day.month == 12
        else first_of_month.replace(month=day.month + 1)
    )

    return LocalFrame(
        day_start=day_start,
        day_end=_midnight(day + timedelta(days=1)),
        week_start=_midnight(first_weekday),
        week_end=_midnight(first_weekday + timedelta(days=7)),
        month_start=_midnight(first_of_month),
        month_end=_midnight(next_month),
        weekday=day.weekday(),
        start_minutes=math.floor((start_at - day_start).total_seconds() / 60),
        end_minutes=math.ceil((end_at - day_start).total_seconds() / 60),
    )
