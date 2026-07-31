"""Tests for the local-hours-to-UTC boundary conversion in `app.operating_hours`.

Every case here is a pure function call — no fixture, no `driver`, no
`DATABASE_URL` — so this module runs whether or not Postgres is up, unlike most
of this suite. That is load-bearing for the module under test, not incidental
to these tests: `resolve_operating_hours` is deliberately dependency-free so
task 4.13 can call it from the `rules_stub` boundary without pulling in the ORM,
and a test suite that silently needed a database would contradict that.
"""

from datetime import date, time

import pytest

from app.operating_hours import resolve_operating_hours


def test_europe_berlin_is_two_hours_ahead_in_july_cest():
    """07:00 Europe/Berlin in July is 05:00Z — CEST, UTC+2.

    The headline assertion: DST-correctness is the entire point of this
    module, and this is the exact example from the decisions table
    ("07:00 Europe/Berlin on 2026-07-21 -> 05:00Z").
    """
    utc_open, utc_close = resolve_operating_hours(
        opens_at=time(7, 0),
        closes_at=time(23, 0),
        tz_name="Europe/Berlin",
        on_date=date(2026, 7, 21),
    )
    assert utc_open == time(5, 0)
    assert utc_close == time(21, 0)


def test_europe_berlin_is_one_hour_ahead_in_january_cet():
    """The *same* wall-clock 07:00 is 06:00Z in January — CET, UTC+1.

    Same local input as the July case above, different `on_date`, different
    UTC answer. That contrast is what a fixed-offset column could never
    reproduce: it would be right for one of these two calls and wrong for
    the other, silently, depending only on which month someone tested it in.
    """
    utc_open, utc_close = resolve_operating_hours(
        opens_at=time(7, 0),
        closes_at=time(23, 0),
        tz_name="Europe/Berlin",
        on_date=date(2026, 1, 21),
    )
    assert utc_open == time(6, 0)
    assert utc_close == time(22, 0)


def test_utc_space_hours_are_unchanged():
    """A Space on UTC itself is the identity conversion — no offset, no DST."""
    utc_open, utc_close = resolve_operating_hours(
        opens_at=time(6, 0),
        closes_at=time(23, 0),
        tz_name="UTC",
        on_date=date(2026, 7, 21),
    )
    assert utc_open == time(6, 0)
    assert utc_close == time(23, 0)


def test_fractional_offset_zone_resolves_correctly():
    """Asia/Kolkata is UTC+5:30 year-round (no DST) — a half-hour offset,
    not a whole one, so this catches an implementation that only ever
    subtracts whole hours."""
    utc_open, utc_close = resolve_operating_hours(
        opens_at=time(9, 0),
        closes_at=time(21, 0),
        tz_name="Asia/Kolkata",
        on_date=date(2026, 7, 21),
    )
    assert utc_open == time(3, 30)
    assert utc_close == time(15, 30)


def test_unknown_timezone_name_fails_loudly():
    """An invalid IANA name is never silently treated as UTC.

    `ZoneInfo` raises `zoneinfo.ZoneInfoNotFoundError` (a `KeyError`
    subclass) for a name its tzdata does not recognise, and this module lets
    that propagate rather than catching it and falling back — a fallback
    here would silently open or close a venue at the wrong instant with no
    visible error. Validating a zone name a human typed is task 4.12's job;
    this only asserts the fail-loud contract for a bad one reaching here.
    """
    with pytest.raises(KeyError):
        resolve_operating_hours(
            opens_at=time(7, 0),
            closes_at=time(23, 0),
            tz_name="Not/AZone",
            on_date=date(2026, 7, 21),
        )


def test_a_utc_day_crossing_window_resolves_to_an_inverted_pair():
    """A window whose UTC images cross a calendar day is returned, not refused.

    Pacific/Auckland is UTC+13 in the New Zealand summer, so an ordinary
    06:00-23:00 local window resolves to 17:00Z on the *previous* UTC date
    through 10:00Z on `on_date` — returned here as `(17:00, 10:00)`, an
    inverted `(open, close)` pair by string comparison. That inversion is
    exactly how `rules.canon.AvailabilityHoursRule` reads "this window spans
    two UTC dates" (see its own docstring and the module docstring here);
    this function used to raise `MidnightWrapError` instead, which made an
    entirely ordinary local schedule unrepresentable (`DEFERRED.md` item 17).
    """
    utc_open, utc_close = resolve_operating_hours(
        opens_at=time(6, 0),
        closes_at=time(23, 0),
        tz_name="Pacific/Auckland",
        on_date=date(2026, 1, 21),
    )
    assert utc_open == time(17, 0)
    assert utc_close == time(10, 0)


def test_utc_day_crossing_is_not_only_a_utc_plus_13_problem():
    """An ordinary 09:00-17:00 window crosses at UTC+10 too, not just UTC+13/+14.

    Found by task 5.1 verifying the seeded sandbox end to end: Australia/Sydney
    sits at UTC+10 (AEST), so 09:00 local resolves to 23:00Z on the *previous*
    UTC date while 17:00 resolves to 07:00Z the same day — the same crossing
    the Auckland case above demonstrates, just at a smaller offset. The
    crossing condition is `opens_at_hour < utc_offset_hours`, which this
    module's own docstring used to undersell by naming only "roughly UTC-11
    to UTC+12" and UTC+13/+14 as the practical case; an ordinary 9-to-5
    schedule at UTC+10 hits it too.
    """
    utc_open, utc_close = resolve_operating_hours(
        opens_at=time(9, 0),
        closes_at=time(17, 0),
        tz_name="Australia/Sydney",
        on_date=date(2026, 7, 29),
    )
    assert utc_open == time(23, 0)
    assert utc_close == time(7, 0)


def test_sydney_09_to_21_crosses_a_utc_day_in_either_season():
    """The sandbox seed's real Space B hours (09:00-21:00) now resolve, crossing in both seasons.

    Sydney alternates AEST (UTC+10) and AEDT (UTC+11) across the year; opening
    at 09:00 sits behind the offset in both, so both seasons cross a UTC
    calendar-day boundary — this is the exact configuration that made Space B
    unbookable before this module stopped raising (`DEFERRED.md` item 17).
    """
    winter_open, winter_close = resolve_operating_hours(
        opens_at=time(9, 0),
        closes_at=time(21, 0),
        tz_name="Australia/Sydney",
        on_date=date(2026, 7, 29),  # AEST, UTC+10
    )
    assert winter_open == time(23, 0)
    assert winter_close == time(11, 0)

    summer_open, summer_close = resolve_operating_hours(
        opens_at=time(9, 0),
        closes_at=time(21, 0),
        tz_name="Australia/Sydney",
        on_date=date(2026, 1, 15),  # AEDT, UTC+11
    )
    assert summer_open == time(22, 0)
    assert summer_close == time(10, 0)


def test_honolulu_08_to_20_crosses_a_utc_day_year_round():
    """Pacific/Honolulu has no DST (always UTC-10), so this crosses every date the same way."""
    for on_date in (date(2026, 1, 15), date(2026, 7, 15)):
        utc_open, utc_close = resolve_operating_hours(
            opens_at=time(8, 0),
            closes_at=time(20, 0),
            tz_name="Pacific/Honolulu",
            on_date=on_date,
        )
        assert utc_open == time(18, 0)
        assert utc_close == time(6, 0)
