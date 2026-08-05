"""Tests for the rule-engine adapter, focused on the boundary directions.

`app.rules_stub` decides nothing itself — it assembles a canon from a
`SpaceRuleConfig` (a Space's timezone plus its `space_rules` rows, read
through `rules.REGISTRY`, task 6.6) and hands it to `rules.evaluate_request`.
These cases are kept pointed at the observable verdict rather than at the
adapter's internals: the individual rules' own edge cases (inclusive bounds,
ordering, window arithmetic) are `rules/tests`' job, so this module asserts
the things that are genuinely this adapter's — timezone conversion, canon
assembly from a `SpaceRuleConfig`'s rows, history forwarding, and (since
task 6.6) the fail-closed path a row that cannot be built takes.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from rules import RULE_ERROR_MESSAGE

from app.rules_stub import (
    ALLOWED_MESSAGE,
    AVAILABILITY_CLOSE,
    AVAILABILITY_OPEN,
    BOOKING_HORIZON_DAYS,
    MAX_BOOKING_DURATION,
    BookingRequest,
    DaySchedule,
    RuleResult,
    SpaceRuleConfig,
    SpaceRuleRow,
    resolve_day_schedule,
)
from app.rules_stub import _build_local_frame, _engine_request, _local_date
from app.rules_stub import evaluate as _evaluate

DAY = datetime(2026, 7, 20, tzinfo=timezone.utc)

# A clock pinned a day before DAY, so every fixed date in this module sits inside
# the booking horizon. Without this the date rules would judge these cases
# against the wall clock and the whole file would start failing on 2026-07-21.
NOW = DAY - timedelta(days=1)


def _config(timezone_name: str = "UTC", **rule_kwargs) -> SpaceRuleConfig:
    """Build a `SpaceRuleConfig` from scalar kwargs, one per rule type.

    Every case in this module cares about the observable verdict, not about
    `space_rules` row shape, so this is a convenience for the tests and
    nothing the API itself has: each non-None kwarg becomes one unscoped,
    enabled `SpaceRuleRow`, with the row's registered param names supplied
    here so a case reads as the configuration it is testing rather than as a
    row literal. `opens_at`/`closes_at` are combined into
    one `availability_hours` row and, matching `_build_canon`'s own gating,
    only when *both* are given — passing just one is exactly how
    `test_availability_needs_both_bounds_set` proves that half a
    configuration enforces nothing.
    """
    rows: list[SpaceRuleRow] = []
    next_id = 1

    def add(rule_type: str, params: dict) -> None:
        nonlocal next_id
        rows.append(SpaceRuleRow(id=next_id, rule_type=rule_type, params=params))
        next_id += 1

    opens_at = rule_kwargs.get("opens_at")
    closes_at = rule_kwargs.get("closes_at")
    if opens_at is not None and closes_at is not None:
        add(
            "availability_hours",
            {"opens_at": opens_at.isoformat(), "closes_at": closes_at.isoformat()},
        )

    if rule_kwargs.get("slot_minutes") is not None:
        add("slot_alignment", {"slot_minutes": rule_kwargs["slot_minutes"]})

    if rule_kwargs.get("max_duration_minutes") is not None:
        add("max_duration", {"max_duration_minutes": rule_kwargs["max_duration_minutes"]})

    if rule_kwargs.get("booking_horizon_days") is not None:
        add("booking_horizon", {"days": rule_kwargs["booking_horizon_days"]})

    if rule_kwargs.get("max_bookings_per_week") is not None:
        add("max_bookings_per_week", {"max_bookings": rule_kwargs["max_bookings_per_week"]})

    if rule_kwargs.get("max_bookings_per_month") is not None:
        add("max_bookings_per_month", {"max_bookings": rule_kwargs["max_bookings_per_month"]})

    return SpaceRuleConfig(timezone=timezone_name, rules=tuple(rows))


#: A Space configured with every rule parameter set, mirroring the values the
#: old module-level ``DEFAULT_CANON`` used to enforce unconditionally — so the
#: cases below that only care about duration/hours/horizon read the same way
#: they did before the canon became per-Space.
FULL_CONFIG = _config(
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
    """A Space with no ``max_duration`` row enforces no duration cap at all."""
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
    """Only one of ``opens_at``/``closes_at`` set produces no ``availability_hours`` row at all."""
    half_configured = _config(opens_at=time(9, 0))
    assert half_configured.rules == ()

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
    berlin_summer = _config("Europe/Berlin", opens_at=time(7, 0), closes_at=time(22, 0))
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
    crosses = _config("Pacific/Auckland", opens_at=time(6, 0), closes_at=time(23, 0))
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
    crosses = _config("Pacific/Auckland", opens_at=time(6, 0), closes_at=time(23, 0))
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
    """No ``max_bookings_per_*`` row means no counting rule in the canon at all."""
    booking = request(at(10), at(11))
    unrelated_history = tuple(request(at(h), at(h + 1)) for h in (1, 2, 3, 12, 13, 14))

    assert evaluate(booking, FULL_CONFIG, unrelated_history).allowed
    assert evaluate(booking, FULL_CONFIG, unrelated_history) == evaluate(booking, FULL_CONFIG, ())


def test_max_bookings_per_week_denies_the_booking_that_goes_over():
    weekly_cap = _config(max_bookings_per_week=2)
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
    weekly_cap = _config(max_bookings_per_week=1)
    history = (request(at(1), at(2), resource_id="a-different-court"),)

    result = evaluate(
        request(at(10), at(11), resource_id="the-requested-court"), weekly_cap, history
    )

    assert not result.allowed


def test_max_bookings_per_month_denies_the_booking_that_goes_over():
    monthly_cap = _config(max_bookings_per_month=1)
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
    config = _config(tz, max_bookings_per_week=1)
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
    config = _config(tz, max_bookings_per_month=1)
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
    config = _config("Australia/Sydney", max_bookings_per_week=1)
    held = _booking(_tz_instant("Australia/Sydney", 2026, 3, 30, 10), minutes=60)
    last_hour = _booking(_tz_instant("Australia/Sydney", 2026, 4, 5, 23, 30))

    result = evaluate(last_hour, config, history=(held,), now=held.start_at)

    assert result.allowed is False


# --- task 7.3: the local frame the adapter resolves for every booking ---------------------------
#
# `Context.local` is where every local question a rule could ask is already answered, as a UTC
# instant or a plain integer, so no rule ever holds a timezone. This adapter is the only thing in
# the system that does hold one, which is why the resolution is asserted here and not in
# `rules/tests` — a suite over there has no zone to be in.
#
# Nothing reads the frame yet (task 7.5 is where a generated rule does), so these go at the
# resolution directly rather than at a verdict. The wiring is covered anyway and from an angle
# these cases cannot reach: `evaluate` attaches the frame and `evaluate_request` cross-checks it
# against the request's own start, so every Sydney and Honolulu case in this module would raise
# `ContextMismatchError` if the adapter resolved a frame for the wrong local date.


def _frame(start: datetime, tz_name: str, minutes: int = 60):
    """The frame `evaluate` would attach to a booking at `start`, resolved exactly as it does."""
    engine_request = _engine_request(_booking(start, minutes=minutes))
    return _build_local_frame(
        engine_request, tz_name, _local_date(engine_request.start_at, tz_name)
    )


def test_a_utc_venue_s_frame_is_the_utc_calendar():
    """The case where local and UTC agree — the baseline the others are read against."""
    frame = _frame(datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc), "UTC")

    assert frame.day_start == datetime(2026, 7, 20, tzinfo=timezone.utc)
    assert frame.day_end == datetime(2026, 7, 21, tzinfo=timezone.utc)
    assert frame.week_start == datetime(2026, 7, 20, tzinfo=timezone.utc)
    assert frame.week_end == datetime(2026, 7, 27, tzinfo=timezone.utc)
    assert frame.month_start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert frame.month_end == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert frame.weekday == 0
    assert frame.start_minutes == 540
    assert frame.end_minutes == 600


def test_an_east_of_utc_venue_s_frame_is_its_own_day_not_the_utc_one():
    """Sydney, Monday 00:30 local — which is *Sunday* 13:30 in UTC.

    This is 5.12's case, asked of the frame rather than of a counting window. Every field
    disagrees with the UTC reading of the same instant, which is what makes it worth pinning:
    `weekday` is Monday and not Sunday, and the day, week and month all begin at 13:00 UTC
    because that is when the venue's day begins.
    """
    local_start = _tz_instant("Australia/Sydney", 2026, 1, 12, 0, 30)
    frame = _frame(local_start, "Australia/Sydney")

    assert local_start.weekday() == 6, "the UTC reading of this instant is a Sunday"
    assert frame.weekday == 0
    assert frame.day_start == datetime(2026, 1, 11, 13, 0, tzinfo=timezone.utc)
    assert frame.day_end == datetime(2026, 1, 12, 13, 0, tzinfo=timezone.utc)
    assert frame.week_start == frame.day_start, "Monday opens the week and the day at once"
    assert frame.week_end == datetime(2026, 1, 18, 13, 0, tzinfo=timezone.utc)
    assert frame.month_start == _tz_instant("Australia/Sydney", 2026, 1, 1)
    assert frame.month_end == _tz_instant("Australia/Sydney", 2026, 2, 1)
    assert frame.start_minutes == 30
    assert frame.end_minutes == 90


def test_a_west_of_utc_venue_s_frame_is_its_own_day_not_the_utc_one():
    """Honolulu, Monday 23:00 local — Tuesday in UTC. The boundary moves the other way."""
    local_start = _tz_instant("Pacific/Honolulu", 2026, 1, 12, 23, 0)
    frame = _frame(local_start, "Pacific/Honolulu")

    assert local_start.weekday() == 1, "the UTC reading of this instant is a Tuesday"
    assert frame.weekday == 0
    assert frame.day_start == datetime(2026, 1, 12, 10, 0, tzinfo=timezone.utc)
    assert frame.day_end == datetime(2026, 1, 13, 10, 0, tzinfo=timezone.utc)
    assert frame.start_minutes == 23 * 60


def test_a_booking_running_past_local_midnight_reports_end_minutes_over_1440():
    """The representation `deferred/passed-midnight.md` needs, and the reason nothing caps it.

    23:00–01:00 Honolulu local starts inside the day and ends outside it. `end_minutes` says so
    rather than wrapping to 60, which would read as a booking that ended 22 hours before it began.
    """
    frame = _frame(_tz_instant("Pacific/Honolulu", 2026, 1, 12, 23, 0), "Pacific/Honolulu", 120)

    assert frame.start_minutes == 1380
    assert frame.end_minutes == 1500


@pytest.mark.parametrize(
    "tz, on, hours",
    [
        # Sydney leaves daylight saving on 5 April 2026: the clocks go back and the day is 25 hours.
        ("Australia/Sydney", (2026, 4, 5, 10), 25),
        # ...and enters it on 4 October, where the same day is 23.
        ("Australia/Sydney", (2026, 10, 4, 10), 23),
        # The northern hemisphere's transitions run the opposite way round, for the same reason.
        ("Europe/Berlin", (2026, 3, 29, 10), 23),
        ("Europe/Berlin", (2026, 10, 25, 10), 25),
        # An ordinary day, so this parametrisation cannot pass by always finding a transition.
        ("Australia/Sydney", (2026, 7, 15, 10), 24),
    ],
)
def test_a_local_day_across_a_dst_transition_is_not_24_hours(tz, on, hours):
    """The assertion that fails if anyone writes ``day_start + timedelta(days=1)``.

    `day_end` is the local midnight of the *next date*, resolved independently, so it lands where
    the venue's next day actually begins. A fixed timedelta would put the boundary an hour inside
    the neighbouring day twice a year — right every time anyone looks, wrong on the two dates that
    matter. This is 5.12's lesson and 6.6's, arriving in a third place.
    """
    frame = _frame(_tz_instant(tz, *on), tz)

    assert frame.day_end - frame.day_start == timedelta(hours=hours)


def test_minutes_from_midnight_are_elapsed_time_not_a_wall_clock():
    """Derived from the instants, which is the whole reason they survive a transition.

    On Sydney's spring-forward date the hour between 02:00 and 03:00 local never happens, so a
    booking at 10:00 local is genuinely nine hours after local midnight and `start_minutes` says
    540. Reading the wall clock instead would say 600 and claim ten hours had elapsed when nine
    had — a frame whose own two halves disagree about the same booking.
    """
    frame = _frame(_tz_instant("Australia/Sydney", 2026, 10, 4, 10, 0), "Australia/Sydney")

    assert frame.start_minutes == 540
    assert frame.day_end - frame.day_start == timedelta(hours=23)


def test_the_frame_carries_no_timezone_however_it_was_resolved():
    """The absence is the invariant, and this is the layer that would leak it.

    Every field above came out of a `ZoneInfo` lookup, so this is the one place a zone could
    plausibly be attached "for debugging" and then be read by a rule that converts with it.
    """
    frame = _frame(_tz_instant("Australia/Sydney", 2026, 1, 12, 0, 30), "Australia/Sydney")

    for forbidden in ("timezone", "tz", "zone", "utc_offset", "offset"):
        assert not hasattr(frame, forbidden), forbidden


# --- task 6.6: rows read through the registry, applies_to, and fail-closed ----------------------


def test_a_disabled_row_is_never_assembled_into_the_canon():
    """Pause is the entire mechanism: a disabled row has no effect at all."""
    paused = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="max_duration",
                params={"max_duration_minutes": 30},
                enabled=False,
            ),
        ),
    )

    # 2 hours would be refused by an *enabled* 30-minute cap; disabled, it is not built at all.
    result = evaluate(request(at(10), at(12)), paused)

    assert result.allowed


def test_applies_to_weekdays_scopes_a_row_to_matching_local_dates():
    """A row scoped to specific weekdays governs only a booking whose *local* date matches.

    2026-07-20 is a Monday (weekday 0). Scoped to Tuesday/Thursday (1, 3), the row must not
    apply, so a booking that a matching day would deny instead passes.
    """
    monday = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="max_duration",
                params={"max_duration_minutes": 30},
                applies_to={"weekdays": [1, 3]},
            ),
        ),
    )

    result = evaluate(request(at(10), at(12)), monday)

    assert result.allowed


def test_applies_to_weekdays_denies_on_a_matching_local_date():
    """The mirror of the case above: the same row, on a date it *does* name."""
    tuesday = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="max_duration",
                params={"max_duration_minutes": 30},
                applies_to={"weekdays": [1]},
            ),
        ),
    )
    a_tuesday = DAY + timedelta(days=1)  # 2026-07-21 is a Tuesday.

    result = evaluate(
        request(a_tuesday + timedelta(hours=10), a_tuesday + timedelta(hours=12)), tuesday
    )

    assert not result.allowed


def test_an_unregistered_rule_type_denies_the_booking_with_the_generic_message():
    """A row naming a rule_type no longer in REGISTRY fails closed, not silently skipped."""
    unknown = SpaceRuleConfig(
        timezone="UTC",
        rules=(SpaceRuleRow(id=1, rule_type="no_such_rule_type", params={}),),
    )

    result = evaluate(request(at(10), at(11)), unknown)

    assert not result.allowed
    assert result.message == RULE_ERROR_MESSAGE


def test_a_row_missing_a_required_param_denies_the_booking_with_the_generic_message():
    """`max_duration` requires `max_duration_minutes`; a row missing it fails closed."""
    broken = SpaceRuleConfig(
        timezone="UTC",
        rules=(SpaceRuleRow(id=1, rule_type="max_duration", params={}),),
    )

    result = evaluate(request(at(10), at(11)), broken)

    assert not result.allowed
    assert result.message == RULE_ERROR_MESSAGE


def test_reads_history_is_true_only_when_an_enabled_row_reads_it():
    weekly = SpaceRuleConfig(
        timezone="UTC",
        rules=(SpaceRuleRow(id=1, rule_type="max_bookings_per_week", params={"max_bookings": 1}),),
    )
    assert weekly.reads_history

    paused = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="max_bookings_per_week",
                params={"max_bookings": 1},
                enabled=False,
            ),
        ),
    )
    assert not paused.reads_history

    assert not NULL_CONFIG.reads_history
    assert not FULL_CONFIG.reads_history


# --- resolve_day_schedule (task 6.9) -----------------------------------------
#
# `GET /spaces/{public_id}/schedule` reads this function's return value
# straight onto the wire, in the Space's own local wall clock, never UTC — so
# these cases build a `SpaceRuleConfig` and read the `DaySchedule` back
# directly, with no `evaluate`/`request()` involved at all.

MONDAY = date(2026, 7, 20)  # Matches DAY above: a Monday.
TUESDAY = date(2026, 7, 21)


def _hours_row(
    row_id: int, opens: str, closes: str, applies_to: dict | None = None
) -> SpaceRuleRow:
    return SpaceRuleRow(
        id=row_id,
        rule_type="availability_hours",
        params={"opens_at": opens, "closes_at": closes},
        applies_to=applies_to,
    )


def _slot_row(row_id: int, minutes: int, applies_to: dict | None = None) -> SpaceRuleRow:
    return SpaceRuleRow(
        id=row_id,
        rule_type="slot_alignment",
        params={"slot_minutes": minutes},
        applies_to=applies_to,
    )


def test_resolve_day_schedule_with_no_rows_is_fully_unconfigured():
    config = SpaceRuleConfig(timezone="UTC", rules=())

    schedule = resolve_day_schedule(config, MONDAY)

    assert schedule == DaySchedule(
        slot_minutes=None, opens_at=None, closes_at=None, coherence_issue=None
    )


def test_resolve_day_schedule_with_one_matching_row_of_each_type():
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(_hours_row(1, "09:00:00", "17:00:00"), _slot_row(2, 30)),
    )

    schedule = resolve_day_schedule(config, MONDAY)

    assert schedule.opens_at == time(9, 0)
    assert schedule.closes_at == time(17, 0)
    assert schedule.slot_minutes == 30
    assert schedule.coherence_issue is None


def test_two_overlapping_availability_rows_intersect_to_a_real_window():
    """9-17 and 12-20 together permit only the overlap, 12-17 — the flat AND."""
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(_hours_row(1, "09:00:00", "17:00:00"), _hours_row(2, "12:00:00", "20:00:00")),
    )

    schedule = resolve_day_schedule(config, MONDAY)

    assert schedule.opens_at == time(12, 0)
    assert schedule.closes_at == time(17, 0)
    assert schedule.coherence_issue is None


def test_two_disjoint_availability_rows_intersect_to_nothing():
    """9-12 and 14-18 permit no overlap at all: closed all day, not a coherence error."""
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(_hours_row(1, "09:00:00", "12:00:00"), _hours_row(2, "14:00:00", "18:00:00")),
    )

    schedule = resolve_day_schedule(config, MONDAY)

    # Normalised to a zero-width window, not reported as inverted or broken.
    assert schedule.opens_at == schedule.closes_at == time(14, 0)
    assert schedule.coherence_issue is None


def test_two_slot_alignment_rows_resolve_to_their_lcm_not_their_minimum():
    """A 20-minute row and a 30-minute row together require a 60-minute grid.

    The minimum (20) would let a booking land on :20 or :40, which the
    30-minute row would still refuse — only multiples of lcm(20, 30) = 60
    satisfy both simultaneously.
    """
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(_slot_row(1, 20), _slot_row(2, 30)),
    )

    schedule = resolve_day_schedule(config, MONDAY)

    assert schedule.slot_minutes == 60


def test_a_row_scoped_to_a_non_matching_weekday_is_excluded():
    """MONDAY is weekday 0; a row scoped to Tuesday/Thursday (1, 3) must not apply."""
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(_hours_row(1, "09:00:00", "17:00:00", applies_to={"weekdays": [1, 3]}),),
    )

    schedule = resolve_day_schedule(config, MONDAY)

    assert schedule.opens_at is None
    assert schedule.closes_at is None


def test_a_row_scoped_to_a_matching_weekday_does_apply():
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(_hours_row(1, "09:00:00", "17:00:00", applies_to={"weekdays": [1]}),),
    )

    schedule = resolve_day_schedule(config, TUESDAY)

    assert schedule.opens_at == time(9, 0)
    assert schedule.closes_at == time(17, 0)


def test_a_disabled_row_is_never_resolved():
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="availability_hours",
                params={"opens_at": "09:00:00", "closes_at": "17:00:00"},
                enabled=False,
            ),
        ),
    )

    schedule = resolve_day_schedule(config, MONDAY)

    assert schedule.opens_at is None
    assert schedule.closes_at is None


def test_coherence_issue_when_hours_do_not_land_on_the_slot_grid():
    """09:15 opening on a 30-minute grid never lands on a boundary."""
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(_hours_row(1, "09:15:00", "17:00:00"), _slot_row(2, 30)),
    )

    schedule = resolve_day_schedule(config, MONDAY)

    assert schedule.coherence_issue is not None
    assert "30-minute slot boundary" in schedule.coherence_issue


def test_a_zero_width_window_is_never_a_coherence_issue_even_with_a_slot_rule():
    """ "Closed all day" plus a slot-alignment row must not also report incoherence."""
    config = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            _hours_row(1, "09:00:00", "12:00:00"),
            _hours_row(2, "14:00:00", "18:00:00"),
            _slot_row(3, 45),
        ),
    )

    schedule = resolve_day_schedule(config, MONDAY)

    assert schedule.opens_at == schedule.closes_at
    assert schedule.coherence_issue is None
