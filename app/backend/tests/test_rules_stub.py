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

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from rules import RULE_ERROR_MESSAGE
from rules import BookingRecord as EngineBookingRecord
from rules import BookingRequest as EngineBookingRequest
from shape import validate_shape

from app.rules_stub import (
    ALLOWED_MESSAGE,
    BOOKING_HORIZON_DAYS,
    MAX_BOOKING_DURATION,
    BookingRequest,
    RuleResult,
    SpaceRuleConfig,
    SpaceRuleRow,
)
from app.rules_stub import (
    _build_local_frame,
    _engine_request,
    _gap_tolerance,
    _local_date,
    _resolve_run,
)
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
    row literal.

    A venue's operating hours and the lengths it offers are no longer rules at
    all — they are its calendar shape (`.claude/rules/calendar-shape.md`),
    enforced by the availability gate one layer above this adapter — so there
    is no kwarg here for either, and the config's own `shape` stays the
    `DEFAULT_SHAPE` every Space starts with unless a case passes its own.
    """
    rows: list[SpaceRuleRow] = []
    next_id = 1

    def add(rule_type: str, params: dict) -> None:
        nonlocal next_id
        rows.append(SpaceRuleRow(id=next_id, rule_type=rule_type, params=params))
        next_id += 1

    if rule_kwargs.get("max_duration_minutes") is not None:
        add("max_duration", {"max_duration_minutes": rule_kwargs["max_duration_minutes"]})

    if rule_kwargs.get("max_consecutive_minutes") is not None:
        add(
            "max_consecutive_duration",
            {"max_consecutive_minutes": rule_kwargs["max_consecutive_minutes"]},
        )

    if rule_kwargs.get("booking_horizon_days") is not None:
        add("booking_horizon", {"days": rule_kwargs["booking_horizon_days"]})

    if rule_kwargs.get("max_bookings_per_day") is not None:
        add("max_bookings_per_day", {"max_bookings": rule_kwargs["max_bookings_per_day"]})

    if rule_kwargs.get("max_duration_per_day_minutes") is not None:
        add(
            "max_duration_per_day",
            {"max_duration_minutes": rule_kwargs["max_duration_per_day_minutes"]},
        )

    if rule_kwargs.get("max_bookings_per_week") is not None:
        add("max_bookings_per_week", {"max_bookings": rule_kwargs["max_bookings_per_week"]})

    if rule_kwargs.get("max_bookings_per_month") is not None:
        add("max_bookings_per_month", {"max_bookings": rule_kwargs["max_bookings_per_month"]})

    return SpaceRuleConfig(timezone=timezone_name, rules=tuple(rows))


#: A Space configured with every rule parameter set, mirroring the values the
#: old module-level ``DEFAULT_CANON`` used to enforce unconditionally — so the
#: cases below that only care about duration or horizon read the same way
#: they did before the canon became per-Space.
FULL_CONFIG = _config(
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


def test_denial_messages_are_human_readable():
    over_long = request(at(10), at(13))
    in_the_past = request(NOW - timedelta(hours=2), NOW - timedelta(hours=1))

    for booking in (over_long, in_the_past):
        message = evaluate(booking).message

        assert message.endswith(".")
        assert message[0].isupper()
        assert "Error" not in message
        assert "Traceback" not in message


def test_non_utc_offsets_are_converted_to_utc_before_evaluation():
    """A booking is judged on the instant it names, not on the client's local spelling.

    The engine rejects a non-zero offset outright, so the adapter converts at
    the boundary. 09:00+07:00 is 02:00Z, an hour *behind* the clock below — so
    the booking is in the past and refused, even though the client's own wall
    clock reads several hours ahead of it.
    """
    local = timezone(timedelta(hours=7))
    start = datetime(2026, 7, 20, 9, 0, tzinfo=local)
    clock = datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc)

    result = evaluate(request(start, start + timedelta(hours=1)), now=clock)

    assert not result.allowed
    assert "already passed" in result.message


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


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        evaluate(request(at(10), at(11)), now=datetime(2026, 7, 19, 10, 0))


def test_clock_defaults_to_the_current_time_when_omitted():
    """The default must be live, or production would judge against a frozen clock."""
    # Pinned to 10:00 so the outcome does not turn on what time of day the
    # suite happens to run.
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


def test_max_bookings_per_day_denies_the_booking_that_goes_over():
    daily_cap = _config(max_bookings_per_day=2)
    history = (
        request(at(1), at(2), resource_id="court-1"),
        request(at(3), at(4), resource_id="court-2"),
    )

    result = evaluate(request(at(10), at(11), resource_id="court-1"), daily_cap, history)

    assert not result.allowed
    assert "a day" in result.message


def test_max_bookings_per_day_counts_across_every_resource_in_the_space():
    daily_cap = _config(max_bookings_per_day=1)
    history = (request(at(1), at(2), resource_id="a-different-court"),)

    result = evaluate(
        request(at(10), at(11), resource_id="the-requested-court"), daily_cap, history
    )

    assert not result.allowed


def test_max_duration_per_day_denies_the_booking_that_goes_over():
    daily_total = _config(max_duration_per_day_minutes=90)
    history = (request(at(1), at(2)),)  # one hour earlier the same day

    result = evaluate(request(at(10), at(11)), daily_total, history)  # +1 hour = 2 hours total

    assert not result.allowed
    assert "a day" in result.message


def test_max_duration_per_day_sums_raw_entries_not_the_merged_run_across_two_resources():
    """The case ``MaxDurationPerDayRule``'s own docstring exists to protect: a user holding two
    Resources at overlapping times. The two history entries below overlap by half an hour and sum
    to 2 hours raw, but would collapse to a 90-minute merged run if this rule made the same
    ``merge_adjoining_spans`` call every other counting rule in this stream does. The cap (110
    minutes) sits strictly between the correct total (135 minutes: 120 history + 15 request) and
    the run-based one (105 minutes), so a rule that merged by mistake would wrongly allow this."""
    daily_total = _config(max_duration_per_day_minutes=110)
    history = (
        request(at(9), at(10), resource_id="court-1"),
        request(at(9, 30), at(10, 30), resource_id="court-2"),
    )

    result = evaluate(request(at(12), at(12, 15)), daily_total, history)

    assert not result.allowed


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


# --- task 8.6: the counting rules count runs, not rows ----------------------------------------
#
# `ops/pending/bugs/max-duration-cannon.md`, "Counting runs, not rows": these rules now resolve
# `context.history` into merged sessions (`_history_of_runs`), the request already folded in, and
# compare directly with no `+1`. The cases above (denied on a row count that never abuts anything)
# hold unchanged — the row count and the run count agree whenever nothing merges — so what is new
# here is exactly the cases where merging changes the verdict: abutment collapsing two rows into
# one session, a request extending a run it already holds, and the "a run counts on the side it
# begins" consequence at a window boundary.


def test_two_abutting_bookings_count_as_one_against_a_weekly_cap():
    """A member who books two abutting hours spends one slot of the cap, not two — the fix task 8.6
    exists for. The two history rows below abut exactly and are drawn from two different Resources,
    which a run merges across by design (`RunContext`)."""
    weekly_cap = _config(max_bookings_per_week=2)
    history = (
        request(at(1), at(2), resource_id="court-1"),
        request(at(2), at(3), resource_id="court-2"),
    )

    result = evaluate(request(at(10), at(11)), weekly_cap, history)

    assert result.allowed


def test_two_separated_bookings_count_as_two_against_a_weekly_cap():
    """Contrast with the abutting case above: with a real gap between them, two bookings are still
    two sessions, so the identical cap that admitted the abutting pair refuses this one."""
    weekly_cap = _config(max_bookings_per_week=2)
    history = (request(at(1), at(2)), request(at(5), at(6)))

    result = evaluate(request(at(10), at(11)), weekly_cap, history)

    assert not result.allowed


def test_a_request_extending_a_held_run_is_allowed_at_a_weekly_cap_the_row_count_would_refuse():
    """The regression task 8.6 exists for. Counting raw rows (``existing + 1 > max_bookings``)
    would have refused this: one existing row plus the request is two, over a cap of one. Counting
    runs instead sees one session, since the request abuts the existing booking exactly."""
    weekly_cap = _config(max_bookings_per_week=1)
    history = (request(at(9), at(10)),)

    result = evaluate(request(at(10), at(11)), weekly_cap, history)

    assert result.allowed


def test_a_weekly_run_starting_before_the_window_does_not_count_even_when_the_request_crosses_in():
    """A run counts on the side it **begins**. A session that started last week, which the request
    merely extends into this one, is still last week's session — it adds nothing to this week's
    count. Mildly surprising, and stated as intentional in `.claude/rules/rule-engine.md`."""
    weekly_cap = _config(max_bookings_per_week=1)
    week_start = DAY  # 2026-07-20 is a Monday
    history = (request(week_start - timedelta(hours=1), week_start, resource_id="court-1"),)

    result = evaluate(
        request(week_start, week_start + timedelta(hours=1), resource_id="court-2"),
        weekly_cap,
        history,
        now=week_start - timedelta(hours=2),
    )

    assert result.allowed


def test_a_weekly_run_starting_exactly_at_the_window_boundary_counts():
    """Contrast with the case above: a run that begins exactly on the boundary belongs to the week
    that opens there, so an unrelated second session the same week still trips the cap."""
    weekly_cap = _config(max_bookings_per_week=1)
    week_start = DAY
    history = (request(week_start, week_start + timedelta(hours=1), resource_id="court-1"),)

    result = evaluate(request(at(10), at(11), resource_id="court-2"), weekly_cap, history)

    assert not result.allowed


def test_two_abutting_bookings_count_as_one_against_a_monthly_cap():
    monthly_cap = _config(max_bookings_per_month=2)
    history = (
        request(at(1), at(2), resource_id="court-1"),
        request(at(2), at(3), resource_id="court-2"),
    )

    result = evaluate(request(at(10), at(11)), monthly_cap, history)

    assert result.allowed


def test_a_request_extending_a_held_run_is_allowed_at_a_monthly_cap_the_row_count_would_refuse():
    """Monthly gets the identical coverage as weekly — do not test one and assume the other."""
    monthly_cap = _config(max_bookings_per_month=1)
    history = (request(at(9), at(10)),)

    result = evaluate(request(at(10), at(11)), monthly_cap, history)

    assert result.allowed


def test_a_monthly_run_starting_before_the_window_does_not_count_even_when_the_request_crosses_in():
    """Mirrors the weekly case: a run counts on the side it begins, so a session that started in
    June and the request merely extends into July adds nothing to July's count."""
    monthly_cap = _config(max_bookings_per_month=1)
    month_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    history = (request(month_start - timedelta(hours=1), month_start, resource_id="court-1"),)

    result = evaluate(
        request(month_start, month_start + timedelta(hours=1), resource_id="court-2"),
        monthly_cap,
        history,
        now=month_start - timedelta(hours=2),
    )

    assert result.allowed


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
    """A one-hour booking starting at ``moment``.

    Anchoring on 10:00 keeps every horizon case clear of the duration rule, so
    a denial here can only have come from a date rule.
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
        # Sydney is UTC+11 in January: a booking at 00:30 local is 13:30 the *previous* day in
        # UTC — the miscount the window-passing design exists to prevent (task 8.7).
        ("Australia/Sydney", (2026, 1, 12, 20), (2026, 1, 12, 0, 30), False),
        # Genuinely different Sydney days stay allowed — the fix must not simply deny more.
        ("Australia/Sydney", (2026, 1, 11, 20), (2026, 1, 12, 0, 30), True),
        # Honolulu is UTC-10, so its boundary moves the other way: 23:00 local is the *next* UTC
        # day.
        ("Pacific/Honolulu", (2026, 1, 12, 23), (2026, 1, 12, 10), False),
        ("Pacific/Honolulu", (2026, 1, 13, 10), (2026, 1, 12, 10), True),
        # A UTC venue is unaffected, which is what makes this a fix rather than a change.
        ("UTC", (2026, 1, 12, 20), (2026, 1, 12, 0, 30), False),
    ],
)
def test_the_daily_window_is_the_space_s_local_day(tz, existing, requested, expect_allowed):
    config = _config(tz, max_bookings_per_day=1)
    held = _booking(_tz_instant(tz, *existing), minutes=60)
    request = _booking(_tz_instant(tz, *requested))

    result = evaluate(request, config, history=(held,), now=request.start_at)

    assert result.allowed is expect_allowed


def test_the_daily_duration_window_is_also_the_space_s_local_day():
    """``max_duration_per_day`` resolves the identical local day as ``max_bookings_per_day`` above
    — a booking earlier the same Sydney day must count toward the total even though it and the
    request sit on different UTC calendar dates."""
    config = _config("Australia/Sydney", max_duration_per_day_minutes=90)
    held = _booking(_tz_instant("Australia/Sydney", 2026, 1, 12, 20), minutes=60)
    req = _booking(_tz_instant("Australia/Sydney", 2026, 1, 12, 0, 30), minutes=60)

    result = evaluate(req, config, history=(held,), now=req.start_at)

    assert not result.allowed


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


# --- _resolve_run (task 8.4) -----------------------------------------------------------
#
# The real risk in this task, per `ops/plans/stream-8/8.4-context-run.md`: this is where the
# gap-tolerance arithmetic and the transitive merge actually live, so these cases build engine
# types directly and call `_resolve_run` itself rather than going through `evaluate`, which would
# only tell us whether a booking passed or failed, not what run it was judged against.

RUN_USER = "u1"


def _erecord(start: datetime, end: datetime, resource_id: str = "court-1") -> EngineBookingRecord:
    return EngineBookingRecord(
        user_id=RUN_USER, resource_id=resource_id, start_at=start, end_at=end
    )


def _erequest(start: datetime, end: datetime, resource_id: str = "court-1") -> EngineBookingRequest:
    return EngineBookingRequest(
        user_id=RUN_USER, resource_id=resource_id, start_at=start, end_at=end
    )


def test_no_history_resolves_the_request_alone():
    req = _erequest(at(10), at(11))

    run = _resolve_run(req, (), timedelta(0))

    assert run.start_at == at(10)
    assert run.end_at == at(11)
    assert run.booking_count == 1


def test_a_booking_abutting_before_and_after_merges_transitively():
    """17-18 and 19-20 already held, requesting 18-19, merges into one 17-20 run — a user can be
    denied by a booking two hops away, and this is that case at the resolver level."""
    req = _erequest(at(18), at(19))
    history = (_erecord(at(17), at(18)), _erecord(at(19), at(20)))

    run = _resolve_run(req, history, timedelta(0))

    assert run.start_at == at(17)
    assert run.end_at == at(20)
    assert run.booking_count == 3


def test_a_booking_neither_abutting_nor_within_tolerance_is_excluded():
    req = _erequest(at(10), at(11))
    far = _erecord(at(13), at(14))  # a 2-hour gap, nowhere near any plausible tolerance

    run = _resolve_run(req, (far,), timedelta(minutes=30))

    assert run.start_at == at(10)
    assert run.end_at == at(11)
    assert run.booking_count == 1


def test_a_five_minute_gap_joins_when_the_tolerance_covers_it():
    """The clause `max-duration-cannon.md`'s decision 3 attaches: a gap shorter than the shortest
    length this Space's shape offers is dead space nobody could ever book, so it must not fracture
    the run and hand every run-based rule a free escape hatch."""
    req = _erequest(at(10), at(11))
    adjoining = _erecord(at(11, 5), at(12, 5))

    run = _resolve_run(req, (adjoining,), timedelta(minutes=60))

    assert run.start_at == at(10)
    assert run.end_at == at(12, 5)
    assert run.booking_count == 2


def test_the_same_five_minute_gap_does_not_join_with_no_tolerance():
    """A date this Space's shape offers nothing on gets `tolerance == timedelta(0)` — exact
    abutment, `max-duration-cannon.md`'s original decision 3, unchanged for a date the venue is
    not open on at all."""
    req = _erequest(at(10), at(11))
    adjoining = _erecord(at(11, 5), at(12, 5))

    run = _resolve_run(req, (adjoining,), timedelta(0))

    assert run.start_at == at(10)
    assert run.end_at == at(11)
    assert run.booking_count == 1


def test_two_resources_abutting_still_merge_into_one_run():
    """The cross-court circumvention case, at the resolver level: `history` is already filtered to
    the user and never to one resource (`interfaces.HistoryContext`), so a booking on a *different*
    court still joins the run — a member cannot dodge a run-aware cap by switching courts. 8.9 is
    this same property's E2E guard."""
    req = _erequest(at(10), at(11), resource_id="court-1")
    other_court = _erecord(at(11), at(12), resource_id="court-2")

    run = _resolve_run(req, (other_court,), timedelta(0))

    assert run.start_at == at(10)
    assert run.end_at == at(12)
    assert run.booking_count == 2


def test_exact_abutment_joins_with_zero_tolerance():
    """The bound case `max-duration-cannon.md`'s decision 3 names directly: zero gap always joins,
    tolerance or none."""
    req = _erequest(at(11), at(12))
    before = _erecord(at(10), at(11))

    run = _resolve_run(req, (before,), timedelta(0))

    assert run.start_at == at(10)
    assert run.end_at == at(12)
    assert run.booking_count == 2


def test_an_overlap_across_two_resources_joins_even_with_zero_tolerance():
    """A negative gap (an overlap) joins unconditionally — reachable once history is drawn from
    more than one Resource, since two Resources' bookings are never mutually exclusive in time the
    way two bookings on one Resource are."""
    req = _erequest(at(10), at(12), resource_id="court-1")
    overlapping = _erecord(at(11), at(13), resource_id="court-2")

    run = _resolve_run(req, (overlapping,), timedelta(0))

    assert run.start_at == at(10)
    assert run.end_at == at(13)
    assert run.booking_count == 2


# --- max_consecutive_duration through evaluate (task 8.5) -------------------------------------
#
# `_resolve_run`'s own tests above already prove the resolver merges transitively and across
# Resources; what belongs here is the property that is genuinely this adapter's own — that a Space
# configuring *only* `max_consecutive_duration` still gets the Space-wide history query the rule's
# verdict silently depends on, end to end through `evaluate`, not just at the resolver level.


def test_max_consecutive_duration_denies_a_run_across_two_resources():
    """The bug report itself (`ops/pending/bugs/max-duration-cannon.md`), through the real adapter:
    one hour already held on one court, a request for the adjoining hour (plus a minute) on a
    *different* court. The cross-court circumvention concern the bug's own testing note raises is
    already closed by `HistoryContext` being filtered to the user, never to one resource — proven
    here by the two bookings landing on different courts and the run still catching them."""
    consecutive_cap = _config(max_consecutive_minutes=120)
    history = (request(at(9), at(10), resource_id="court-1"),)

    result = evaluate(request(at(10), at(11, 1), resource_id="court-2"), consecutive_cap, history)

    assert not result.allowed
    assert "consecutive" in result.message


def test_max_consecutive_duration_allows_a_run_of_exactly_the_cap():
    """The bound is inclusive, mirroring `MaxDurationRule`'s own convention: a run of exactly the
    cap passes, and this is also the "no neighbour" half of the bug-report pair — the identical
    request, on its own (no history at all), passes too."""
    consecutive_cap = _config(max_consecutive_minutes=120)
    history = (request(at(9), at(10), resource_id="court-1"),)

    joined = evaluate(request(at(10), at(11), resource_id="court-2"), consecutive_cap, history)
    assert joined.allowed

    solo = evaluate(request(at(10), at(11), resource_id="court-2"), consecutive_cap, ())
    assert solo.allowed


def test_a_space_holding_only_max_consecutive_duration_still_reads_history():
    """The property that makes this rule work at all (`rules/rules/registry.py`,
    `.claude/rules/rule-engine.md`): `MaxConsecutiveDurationRule.evaluate` never names
    `context.history`, only `context.run`, but the run itself is resolved from history by this
    adapter — so a Space configuring this rule and nothing else that reads history must still make
    the router run the Space-wide history query. Without it the run the rule sees is always the
    request alone and it would silently never deny."""
    consecutive_only = SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="max_consecutive_duration",
                params={"max_consecutive_minutes": 120},
            ),
        ),
    )
    assert consecutive_only.reads_history


# --- task 10.5: the gap tolerance, re-sourced from the calendar shape ---------------------------
#
# THE HAZARD, and the whole reason these cases are written by hand.
#
# Retiring `session_length` cut the source `_resolve_run`'s merge tolerance used to come from
# (`resolve_day_schedule(config, on_date).session_minutes`). Left un-re-sourced, the tolerance
# becomes `timedelta(0)` everywhere, `merge_adjoining_spans` stops merging across any non-abutting
# gap, and every run-based rule — `max_consecutive_duration` and all three counting rules —
# quietly loosens. **Nothing raises, and no test that existed before this section fails**, which
# is exactly why the coverage has to be added deliberately rather than assumed: a cap simply stops
# catching what it caught. `.claude/rules/calendar-shape.md`, "The run's gap tolerance is
# re-sourced from the projection", holds the argument these cases pin.

#: A Monday, matching `DAY` above.
SHAPE_MONDAY = date(2026, 7, 20)
#: The Tuesday after it — a date `MONDAY_ONLY_SHAPE` offers nothing on.
SHAPE_TUESDAY = date(2026, 7, 21)


def _shape(days: list[str], durations: list[int]):
    """A shape open 09:00-21:00 on ``days``, offering ``durations``."""
    return validate_shape(
        {
            "version": 1,
            "operating_blocks": [
                {
                    "days": days,
                    "start_time": "09:00",
                    "end_time": "21:00",
                    "allowed_durations_mins": durations,
                }
            ],
            "blackout_windows": [],
        }
    )


HOUR_GRID_SHAPE = _shape(["MON"], [60])
MONDAY_ONLY_SHAPE = HOUR_GRID_SHAPE
HALF_HOUR_AND_HOUR_SHAPE = _shape(["MON"], [30, 60])


def _consecutive_cap_config(shape) -> SpaceRuleConfig:
    """A Space capping consecutive play at two hours, on ``shape``.

    `max_consecutive_duration` is the observable this section reads the tolerance through: its
    verdict depends on the run, the run depends on the tolerance, so a tolerance that silently
    went to zero shows up here as a booking allowed where it should have been refused.
    """
    return SpaceRuleConfig(
        timezone="UTC",
        rules=(
            SpaceRuleRow(
                id=1,
                rule_type="max_consecutive_duration",
                params={"max_consecutive_minutes": 120},
            ),
        ),
        shape=shape,
    )


def test_a_gap_under_the_shapes_shortest_offered_duration_merges_the_run():
    """The silent-loosening case itself, end to end through `evaluate`.

    One hour held, and a request for the hour starting five minutes after it ends. The shape
    offers only 60-minute bookings, so a 5-minute gap is dead space nobody could construct a
    booking to fill: the two are one 2h05 run and the two-hour cap refuses it. With the tolerance
    left un-re-sourced at zero, the run would be the request alone at one hour and this booking
    would be allowed.
    """
    config = _consecutive_cap_config(HOUR_GRID_SHAPE)
    history = (request(at(9), at(10)),)

    result = evaluate(request(at(10, 5), at(11, 5)), config, history)

    assert not result.allowed
    assert "consecutive" in result.message


def test_a_gap_of_exactly_the_shortest_offered_duration_does_not_merge():
    """The other side of the same bound. `merge_adjoining_spans` joins on `gap < tolerance`
    **strictly**, so a gap a legal booking could actually occupy — exactly one offered length — is
    a real gap and fractures the run, which is the correct answer rather than a leak."""
    config = _consecutive_cap_config(HOUR_GRID_SHAPE)
    history = (request(at(9), at(10)),)

    result = evaluate(request(at(11), at(12, 5)), config, history)

    assert result.allowed


def test_the_tolerance_is_the_smallest_offered_duration_not_the_largest():
    """A shape offering 30- and 60-minute bookings resolves **30**, the smallest.

    The largest would merge across a 45-minute gap a member could genuinely have booked into,
    which is the over-strict direction; the first-listed would be an accident of document order.
    """
    config = _consecutive_cap_config(HALF_HOUR_AND_HOUR_SHAPE)

    assert _gap_tolerance(config, SHAPE_MONDAY) == timedelta(minutes=30)


def test_a_date_the_shape_offers_nothing_on_resolves_a_zero_tolerance():
    """No operating interval means no bookable length, so there is no gap a booking could occupy
    and nothing to close: exact abutment, `max-duration-cannon.md`'s original decision 3."""
    config = _consecutive_cap_config(MONDAY_ONLY_SHAPE)

    assert _gap_tolerance(config, SHAPE_TUESDAY) == timedelta(0)


def test_the_default_shape_resolves_its_own_offered_duration():
    """A `SpaceRuleConfig` built without a shape behaves like the fresh Space it stands for.

    `SpaceRuleConfig.shape` defaults to `DEFAULT_SHAPE` — open every day, 60-minute bookings —
    rather than to `None`, because an absent shape would resolve to a tolerance of zero, and zero
    is the *permissive* direction.
    """
    assert _gap_tolerance(SpaceRuleConfig(timezone="UTC"), SHAPE_MONDAY) == timedelta(minutes=60)
