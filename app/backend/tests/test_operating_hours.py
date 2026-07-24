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

from app.operating_hours import MidnightWrapError, resolve_operating_hours


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


def test_midnight_wrap_raises_a_clear_domain_error():
    """A window that wraps past midnight in UTC is refused, not returned inverted.

    Pacific/Auckland is UTC+13 in the New Zealand summer, so an ordinary
    06:00-23:00 local window resolves to 17:00Z on the *previous* UTC date
    through 10:00Z on `on_date` — an inverted (open, close) pair if returned
    as bare `time` values. Raising is the deliberate, documented choice made
    in `MidnightWrapError` rather than handing the engine a window that
    silently never admits a booking.
    """
    with pytest.raises(MidnightWrapError):
        resolve_operating_hours(
            opens_at=time(6, 0),
            closes_at=time(23, 0),
            tz_name="Pacific/Auckland",
            on_date=date(2026, 1, 21),
        )
