"""Tests for the rule-engine adapter, focused on the boundary directions.

`app.rules_stub` decides nothing itself — it assembles a canon from a
`SpaceRuleConfig` and hands it to `rules.evaluate_request`. These cases are
kept pointed at the observable verdict rather than at the adapter's
internals: the individual rules' own edge cases (inclusive bounds, ordering,
window arithmetic) are `rules/tests`' job, so this module asserts the things
that are genuinely this adapter's — timezone conversion, canon assembly from
a `SpaceRuleConfig`, and history forwarding.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.rules_stub import (
    ALLOWED_MESSAGE,
    AVAILABILITY_CLOSE,
    AVAILABILITY_OPEN,
    BOOKING_HORIZON_DAYS,
    MAX_BOOKING_DURATION,
    BookingRequest,
    RuleResult,
    SpaceRuleConfig,
)
from app.rules_stub import evaluate as _evaluate

DAY = datetime(2026, 7, 20, tzinfo=timezone.utc)

# A clock pinned a day before DAY, so every fixed date in this module sits inside
# the booking horizon. Without this the date rules would judge these cases
# against the wall clock and the whole file would start failing on 2026-07-21.
NOW = DAY - timedelta(days=1)

#: A Space configured with every rule parameter set, mirroring the values the
#: old module-level ``DEFAULT_CANON`` used to enforce unconditionally — so the
#: cases below that only care about duration/hours/horizon read the same way
#: they did before the canon became per-Space.
FULL_CONFIG = SpaceRuleConfig(
    timezone="UTC",
    opens_at=AVAILABILITY_OPEN,
    closes_at=AVAILABILITY_CLOSE,
    max_duration_minutes=int(MAX_BOOKING_DURATION.total_seconds() // 60),
    booking_horizon_days=BOOKING_HORIZON_DAYS,
)

#: A Space with every rule parameter left unset — only ``NotInThePastRule``
#: is ever enforced against it.
NULL_CONFIG = SpaceRuleConfig(timezone="UTC")


def evaluate(
    booking: BookingRequest,
    config: SpaceRuleConfig = FULL_CONFIG,
    history: tuple[BookingRequest, ...] = (),
    now: datetime | None = NOW,
) -> RuleResult:
    """``rules_stub.evaluate`` pinned to ``NOW`` and ``FULL_CONFIG`` unless overridden.

    Cases that are *about* the clock or the config pass their own; everything
    else gets the values that keep this module's dates and durations inside
    every configured bound.
    """
    return _evaluate(booking, config, history, now=now)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


def request(start: datetime, end: datetime, resource_id: str | None = None) -> BookingRequest:
    kwargs = {} if resource_id is None else {"resource_id": resource_id}
    return BookingRequest(start_at=start, end_at=end, **kwargs)


def test_booking_inside_hours_and_under_max_duration_is_allowed():
    result = evaluate(request(at(10), at(11)))

    assert result.allowed
    assert result.message


def test_the_allow_path_carries_the_friendly_message():
    """The success banner must not ship blank.

    The engine's own `RuleResult` drops copy when it passes — `passed=True`
    implies `fail_reason is None` — so the adapter has to supply this itself.
    Asserting the exact string, not merely a truthy one: `routers/bookings.py`
    passes `message` straight through to the client on the allow path, and an
    empty one is a UI bug no denial-path test would ever catch.
    """
    assert evaluate(request(at(10), at(11))).message == ALLOWED_MESSAGE


def test_booking_longer_than_max_duration_is_denied():
    result = evaluate(request(at(10), at(13)))

    assert not result.allowed
    assert "2 hours" in result.message


def test_booking_of_exactly_max_duration_is_allowed():
    """The duration limit is inclusive: exactly 2 hours is fine, 2h01 is not."""
    start = at(10)

    assert evaluate(request(start, start + MAX_BOOKING_DURATION)).allowed
    assert not evaluate(request(start, start + MAX_BOOKING_DURATION + timedelta(minutes=1))).allowed


def test_max_duration_unset_allows_a_booking_the_default_would_deny():
    """A Space with no ``max_duration_minutes`` enforces no duration cap at all."""
    over_the_reference_default = request(at(10), at(10) + MAX_BOOKING_DURATION + timedelta(hours=1))

    result = evaluate(over_the_reference_default, NULL_CONFIG)

    assert result.allowed


def test_booking_starting_before_opening_is_denied():
    result = evaluate(request(at(5), at(6, 30)))

    assert not result.allowed
    assert "06:00" in result.message


def test_booking_starting_exactly_at_opening_is_allowed():
    """The opening bound is inclusive: 06:00 is open, 05:59 is not."""
    start = datetime.combine(DAY.date(), AVAILABILITY_OPEN, timezone.utc)

    assert evaluate(request(start, start + timedelta(hours=1))).allowed
    assert not evaluate(
        request(start - timedelta(minutes=1), start + timedelta(minutes=30))
    ).allowed


def test_booking_ending_after_closing_is_denied():
    result = evaluate(request(at(22), at(23, 30)))

    assert not result.allowed
    assert "23:00" in result.message


def test_booking_ending_exactly_at_closing_is_allowed():
    """The closing bound is inclusive: ending at 23:00 is fine, 23:01 is not."""
    closing = datetime.combine(DAY.date(), AVAILABILITY_CLOSE, timezone.utc)

    assert evaluate(request(closing - timedelta(hours=1), closing)).allowed
    assert not evaluate(
        request(closing - timedelta(hours=1), closing + timedelta(minutes=1))
    ).allowed


def test_availability_needs_both_bounds_set():
    """Only one of ``opens_at``/``closes_at`` set enforces no availability rule."""
    half_configured = SpaceRuleConfig(timezone="UTC", opens_at=time(9, 0))

    # 03:00 would be refused under FULL_CONFIG's 06:00 opening; here it passes
    # because the rule is never built without both bounds.
    result = evaluate(request(at(3), at(3, 30)), half_configured)

    assert result.allowed


def test_booking_running_past_midnight_is_denied():
    """A wrap-around must not look like an early-morning booking inside hours."""
    result = evaluate(request(at(22, 30), at(24, 30)))

    assert not result.allowed
    assert "23:00" in result.message


def test_duration_is_checked_before_availability_hours():
    """An over-long booking that is also out of hours reports the length first."""
    result = evaluate(request(at(22), at(25)))

    assert not result.allowed
    assert "2 hours" in result.message


def test_denial_messages_are_human_readable():
    for booking in (request(at(10), at(13)), request(at(5), at(5, 30))):
        message = evaluate(booking).message

        assert message.endswith(".")
        assert message[0].isupper()
        assert "Error" not in message
        assert "Traceback" not in message


def test_non_utc_offsets_are_converted_to_utc_before_evaluation():
    """A booking is judged on its UTC wall clock, not the client's local one.

    Availability hours are UTC clock times (`.claude/rules/rule-engine.md`), so
    09:00+07:00 — 02:00 UTC — is before opening and refused, even though the
    client's own clock reads well inside the window.
    """
    local = timezone(timedelta(hours=7))
    start = datetime(2026, 7, 20, 9, 0, tzinfo=local)

    result = evaluate(request(start, start + timedelta(hours=1)))

    assert not result.allowed
    assert "06:00" in result.message


def test_offset_and_utc_spellings_of_one_instant_agree():
    """The conversion is a change of spelling, not of verdict.

    13:00+07:00 and 06:00Z are the same instant, so they must draw the same
    answer — which is what makes the rule about the moment booked rather than
    about how the client chose to serialise it.
    """
    local = timezone(timedelta(hours=7))
    offset_spelling = datetime(2026, 7, 20, 13, 0, tzinfo=local)
    utc_spelling = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)

    assert offset_spelling == utc_spelling

    hour = timedelta(hours=1)
    assert evaluate(request(offset_spelling, offset_spelling + hour)).allowed
    assert evaluate(request(utc_spelling, utc_spelling + hour)).allowed


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        BookingRequest(start_at=datetime(2026, 7, 20, 10), end_at=at(11))


def test_non_positive_interval_is_rejected():
    with pytest.raises(ValueError):
        BookingRequest(start_at=at(11), end_at=at(11))


# --- Per-Space timezone resolution of availability hours (task 4.13b) ------


def test_availability_hours_resolve_against_the_spaces_own_timezone():
    """A Space's ``opens_at``/``closes_at`` are local, not UTC — resolved per date.

    07:00 Europe/Berlin is 05:00Z in July (CEST, UTC+2). A booking at 05:00Z
    sits exactly at that resolved opening; one a minute earlier does not.
    """
    berlin_summer = SpaceRuleConfig(
        timezone="Europe/Berlin", opens_at=time(7, 0), closes_at=time(22, 0)
    )
    opening_instant = datetime(2026, 7, 20, 5, 0, tzinfo=timezone.utc)

    assert evaluate(
        request(opening_instant, opening_instant + timedelta(hours=1)), berlin_summer
    ).allowed
    denied = evaluate(
        request(
            opening_instant - timedelta(minutes=1),
            opening_instant + timedelta(minutes=30),
        ),
        berlin_summer,
    )
    assert not denied.allowed


def test_a_utc_day_crossing_space_accepts_a_booking_in_its_own_local_hours():
    """A Space whose local hours cross a UTC calendar day is bookable, not dead.

    Pacific/Auckland is UTC+13 in the New Zealand summer (January), so an
    ordinary 06:00-23:00 local window resolves (see
    `app.operating_hours`) to opening 2026-01-20T17:00Z and closing
    2026-01-21T10:00Z — no longer a `MidnightWrapError`. A booking sitting in
    the local morning — 2026-01-21 07:00 Auckland local, which is
    2026-01-20T18:00Z — is accepted, not refused with the engine's generic
    "couldn't check this" copy (`DEFERRED.md` items 16 and 17).
    """
    crosses = SpaceRuleConfig(
        timezone="Pacific/Auckland", opens_at=time(6, 0), closes_at=time(23, 0)
    )
    local_morning = datetime(2026, 1, 20, 18, 0, tzinfo=timezone.utc)

    result = evaluate(
        request(local_morning, local_morning + timedelta(hours=1)),
        crosses,
        now=local_morning - timedelta(hours=1),
    )

    assert result.allowed


def test_a_utc_day_crossing_space_still_denies_an_out_of_hours_booking():
    """The fix is not "widen the window until everything passes" — out-of-hours still denies.

    The denial copy names the engine's own UTC clock (17:00), not the Space's local 06:00 — the
    engine has no timezone to convert from, and rendering a bound in a viewer's own zone stays the
    UI's job (`.claude/rules/rule-engine.md`); unaffected by this task.
    """
    crosses = SpaceRuleConfig(
        timezone="Pacific/Auckland", opens_at=time(6, 0), closes_at=time(23, 0)
    )
    # 2026-01-21 02:00 Auckland local (before the 06:00 local opening) is 2026-01-20T13:00Z.
    before_opening = datetime(2026, 1, 20, 13, 0, tzinfo=timezone.utc)

    result = evaluate(
        request(before_opening, before_opening + timedelta(hours=1)),
        crosses,
        now=before_opening - timedelta(hours=1),
    )

    assert not result.allowed
    assert "17:00" in result.message


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        evaluate(request(at(10), at(11)), now=datetime(2026, 7, 19, 10, 0))


def test_clock_defaults_to_the_current_time_when_omitted():
    """The default must be live, or production would judge against a frozen clock."""
    # Pinned to 10:00 so the availability-hours rule can't decide the outcome
    # when the suite happens to run late at night.
    midmorning = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    too_far_in_the_past = request(
        midmorning - timedelta(days=2), midmorning - timedelta(days=2) + timedelta(hours=1)
    )
    comfortably_ahead = request(
        midmorning + timedelta(days=1), midmorning + timedelta(days=1) + timedelta(hours=1)
    )

    assert not _evaluate(too_far_in_the_past, FULL_CONFIG).allowed
    assert _evaluate(comfortably_ahead, FULL_CONFIG).allowed


# --- Space-wide history and the frequency-cap rules (task 4.13b) -----------


def test_a_space_with_no_frequency_cap_ignores_history_entirely():
    """No ``max_bookings_per_*`` set means no counting rule in the canon at all."""
    booking = request(at(10), at(11))
    unrelated_history = tuple(request(at(h), at(h + 1)) for h in (1, 2, 3, 12, 13, 14))

    assert evaluate(booking, FULL_CONFIG, unrelated_history).allowed
    assert evaluate(booking, FULL_CONFIG, unrelated_history) == evaluate(booking, FULL_CONFIG, ())


def test_max_bookings_per_week_denies_the_booking_that_goes_over():
    weekly_cap = SpaceRuleConfig(timezone="UTC", max_bookings_per_week=2)
    history = (
        request(at(1), at(2), resource_id="court-1"),
        request(at(3), at(4), resource_id="court-2"),
    )

    result = evaluate(request(at(10), at(11), resource_id="court-1"), weekly_cap, history)

    assert not result.allowed
    assert "a week" in result.message


def test_max_bookings_per_week_counts_across_every_resource_in_the_space():
    """The engine counts everything handed to it; the Space-wide scope is the
    adapter's history query, proven here by history drawn from a resource other
    than the one being requested."""
    weekly_cap = SpaceRuleConfig(timezone="UTC", max_bookings_per_week=1)
    history = (request(at(1), at(2), resource_id="a-different-court"),)

    result = evaluate(
        request(at(10), at(11), resource_id="the-requested-court"), weekly_cap, history
    )

    assert not result.allowed


def test_max_bookings_per_month_denies_the_booking_that_goes_over():
    monthly_cap = SpaceRuleConfig(timezone="UTC", max_bookings_per_month=1)
    history = (request(at(1), at(2)),)

    result = evaluate(request(at(10), at(11)), monthly_cap, history)

    assert not result.allowed
    assert "a month" in result.message


def test_a_null_everywhere_space_enforces_only_not_in_the_past():
    assert evaluate(request(at(10), at(11)), NULL_CONFIG).allowed
    assert evaluate(request(at(10), at(10) + timedelta(hours=10)), NULL_CONFIG).allowed

    past = evaluate(request(NOW - timedelta(hours=1), NOW), NULL_CONFIG)
    assert not past.allowed
    assert "already passed" in past.message


# --- Booking horizon (task 1.4b) -------------------------------------------
#
# Every case below injects an explicit clock. None of them may consult the wall
# clock, or they would pass or fail depending on the day the suite is run.

CLOCK = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)


def hours_from(moment: datetime, hours: int = 1) -> BookingRequest:
    """A one-hour booking starting at ``moment``, inside availability hours.

    Anchoring on 10:00 keeps every horizon case clear of the duration and
    opening-hours rules, so a denial here can only have come from a date rule.
    """
    return request(moment, moment + timedelta(hours=hours))


def test_booking_starting_now_is_allowed():
    """The present instant is bookable — the past bound excludes only what's gone."""
    result = evaluate(hours_from(CLOCK), now=CLOCK)

    assert result.allowed


def test_booking_starting_one_minute_in_the_past_is_denied():
    result = evaluate(hours_from(CLOCK - timedelta(minutes=1)), now=CLOCK)

    assert not result.allowed
    assert "already passed" in result.message


def test_booking_exactly_at_the_horizon_is_allowed():
    """Exactly BOOKING_HORIZON_DAYS ahead is the last bookable instant."""
    result = evaluate(hours_from(CLOCK + timedelta(days=BOOKING_HORIZON_DAYS)), now=CLOCK)

    assert result.allowed


def test_booking_one_minute_past_the_horizon_is_denied():
    start = CLOCK + timedelta(days=BOOKING_HORIZON_DAYS, minutes=1)

    result = evaluate(hours_from(start), now=CLOCK)

    assert not result.allowed
    assert str(BOOKING_HORIZON_DAYS) in result.message


def test_booking_horizon_unset_allows_a_booking_past_the_reference_default():
    far_future = hours_from(CLOCK + timedelta(days=BOOKING_HORIZON_DAYS * 2))

    result = evaluate(far_future, NULL_CONFIG, now=CLOCK)

    assert result.allowed


def test_horizon_message_names_the_limit_from_the_constant():
    """The copy must track BOOKING_HORIZON_DAYS, not restate it as a literal."""
    far = hours_from(CLOCK + timedelta(days=BOOKING_HORIZON_DAYS * 2))

    message = evaluate(far, now=CLOCK).message

    assert f"{BOOKING_HORIZON_DAYS} days" in message


def test_date_rules_are_checked_before_duration_and_hours():
    """A booking that is out of range *and* over-long reports the range first.

    Ordering matters here: "shorten it" is unactionable advice for a booking
    whose real problem is the date, and the engine only ever returns one message.
    """
    over_long_and_too_far = request(
        CLOCK + timedelta(days=BOOKING_HORIZON_DAYS + 1),
        CLOCK + timedelta(days=BOOKING_HORIZON_DAYS + 1, hours=3),
    )
    yesterday = CLOCK - timedelta(days=1)
    over_long_and_past = request(yesterday, yesterday + timedelta(hours=3))

    assert f"{BOOKING_HORIZON_DAYS} days" in evaluate(over_long_and_too_far, now=CLOCK).message
    assert "already passed" in evaluate(over_long_and_past, now=CLOCK).message


def test_date_rule_messages_are_human_readable():
    for booking in (
        hours_from(CLOCK - timedelta(days=1)),
        hours_from(CLOCK + timedelta(days=BOOKING_HORIZON_DAYS + 1)),
    ):
        message = evaluate(booking, now=CLOCK).message

        assert message.endswith(".")
        assert message[0].isupper()
        assert "Error" not in message
        assert "Traceback" not in message


# --- counting windows resolve in the Space's own timezone ---------------------------------------
#
# Task 5.12. The two counting rules used to derive their window by snapping the request to UTC
# midnight, which is wrong for every venue that is not on UTC: a Sydney booking at 00:30 Monday
# local is 13:30 *Sunday* in UTC, so it landed in the previous UTC week and a weekly cap of one
# admitted a second booking in the same Sydney week. The adapter now resolves the local week and
# month and hands the rules a pair of instants.
#
# Each case below is chosen so the local answer and the UTC answer *disagree* — a test where they
# agree would pass just as well against the bug.


def _tz_instant(tz_name: str, *parts: int) -> datetime:
    """A local wall-clock time in ``tz_name``, as the UTC instant the backend would store."""
    return datetime(*parts, tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)


def _booking(start: datetime, minutes: int = 30) -> BookingRequest:
    return BookingRequest(start_at=start, end_at=start + timedelta(minutes=minutes))


@pytest.mark.parametrize(
    "tz, existing, requested, expect_allowed",
    [
        # Sydney is UTC+11 in January: Mon 00:30 local is Sun 13:30 UTC, the previous UTC week.
        ("Australia/Sydney", (2026, 1, 14, 10), (2026, 1, 12, 0, 30), False),
        # ...and UTC+10 in July, so the same case must hold on the other side of the DST change.
        ("Australia/Sydney", (2026, 7, 15, 10), (2026, 7, 13, 0, 30), False),
        # Genuinely different Sydney weeks stay allowed — the fix must not simply deny more.
        ("Australia/Sydney", (2026, 1, 8, 10), (2026, 1, 12, 0, 30), True),
        # Honolulu is UTC-10, so its boundary moves the other way: Sun 23:00 local is Mon UTC.
        ("Pacific/Honolulu", (2026, 1, 12, 23), (2026, 1, 14, 10), False),
        ("Pacific/Honolulu", (2026, 1, 11, 23), (2026, 1, 14, 10), True),
        ("Europe/Berlin", (2026, 1, 14, 10), (2026, 1, 12, 0, 30), False),
        # A UTC venue is unaffected, which is what makes this a fix rather than a change.
        ("UTC", (2026, 1, 14, 10), (2026, 1, 12, 0, 30), False),
    ],
)
def test_the_weekly_window_is_the_space_s_local_week(tz, existing, requested, expect_allowed):
    config = SpaceRuleConfig(timezone=tz, max_bookings_per_week=1)
    held = _booking(_tz_instant(tz, *existing), minutes=60)
    request = _booking(_tz_instant(tz, *requested))

    result = evaluate(request, config, history=(held,), now=request.start_at)

    assert result.allowed is expect_allowed


@pytest.mark.parametrize(
    "tz, existing, requested, now, expect_allowed",
    [
        # Same Sydney month, different UTC months either side of the request.
        ("Australia/Sydney", (2026, 2, 2, 10), (2026, 2, 25, 10), (2026, 2, 1, 0), False),
        # 1 March 00:30 in Sydney is 28 February in UTC — a different local month, so allowed.
        ("Australia/Sydney", (2026, 2, 2, 10), (2026, 3, 1, 0, 30), (2026, 2, 1, 0), True),
        # Honolulu: 31 January 23:00 local is 1 February UTC, still local January.
        ("Pacific/Honolulu", (2026, 1, 2, 10), (2026, 1, 31, 23), (2026, 1, 25, 0), False),
        ("Pacific/Honolulu", (2026, 1, 2, 10), (2026, 2, 1, 10), (2026, 1, 25, 0), True),
        ("UTC", (2026, 2, 2, 10), (2026, 2, 25, 10), (2026, 2, 1, 0), False),
    ],
)
def test_the_monthly_window_is_the_space_s_local_month(
    tz, existing, requested, now, expect_allowed
):
    config = SpaceRuleConfig(timezone=tz, max_bookings_per_month=1)
    held = _booking(_tz_instant(tz, *existing), minutes=60)
    request = _booking(_tz_instant(tz, *requested))

    result = evaluate(request, config, history=(held,), now=_tz_instant(tz, *now))

    assert result.allowed is expect_allowed


def test_a_week_spanning_a_dst_change_is_seven_local_days_long():
    """Not ``start + 7 days``: across a transition the week is 167 or 169 hours.

    Sydney leaves daylight saving on 5 April 2026, so the week of Mon 30 March runs 169 hours. A
    booking late on the final Sunday is inside that week and must count; adding a fixed timedelta
    to the start would close the window an hour early and let it through.
    """
    config = SpaceRuleConfig(timezone="Australia/Sydney", max_bookings_per_week=1)
    held = _booking(_tz_instant("Australia/Sydney", 2026, 3, 30, 10), minutes=60)
    last_hour = _booking(_tz_instant("Australia/Sydney", 2026, 4, 5, 23, 30))

    result = evaluate(last_hour, config, history=(held,), now=held.start_at)

    assert result.allowed is False
