"""Tests for the rule engine's core interfaces.

The point of these types is that invalid states cannot be constructed, so most of what is worth
testing here is a rejection path rather than a happy path.
"""

from datetime import datetime, timedelta, timezone

import pytest

from rules.interfaces import (
    HISTORY_ROLLING_WINDOW,
    BaseRule,
    BookingRecord,
    BookingRequest,
    CalendarContext,
    Context,
    HistoryContext,
    LocalFrame,
    RuleResult,
    UserContext,
    Weekday,
    history_window,
)
from tests.frames import utc_frame

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
NAIVE = datetime(2026, 7, 20, 12, 0)
PLUS_TWO = timezone(timedelta(hours=2))


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def booking(start: datetime, end: datetime, user_id: str = "u1") -> BookingRecord:
    return BookingRecord(user_id=user_id, resource_id="court-1", start_at=start, end_at=end)


def make_context(
    *bookings: BookingRecord,
    now: datetime = NOW,
    week_starts_on: Weekday = Weekday.MONDAY,
) -> Context:
    return Context(
        user=UserContext(user_id="u1"),
        calendar=CalendarContext(week_starts_on=week_starts_on, now=now),
        local=utc_frame(now, week_starts_on=week_starts_on),
        history=HistoryContext(bookings=bookings),
    )


# --------------------------------------------------------------------------------------
# Weekday
# --------------------------------------------------------------------------------------


def test_weekday_matches_datetime_weekday_numbering() -> None:
    # 2026-07-20 is a Monday. Rules do week math against .weekday(), so these must agree.
    assert Weekday.MONDAY == 0
    assert Weekday.SUNDAY == 6
    assert NOW.weekday() == Weekday.MONDAY


# --------------------------------------------------------------------------------------
# Timezone rejection — the same rule applies to every datetime field on every type
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda d: booking(d, utc(2026, 7, 20, 13)), id="record-start"),
        pytest.param(lambda d: booking(utc(2026, 7, 20, 11), d), id="record-end"),
        pytest.param(
            lambda d: BookingRequest("u1", "court-1", d, utc(2026, 7, 20, 13)),
            id="request-start",
        ),
        pytest.param(
            lambda d: BookingRequest("u1", "court-1", utc(2026, 7, 20, 11), d),
            id="request-end",
        ),
        pytest.param(lambda d: CalendarContext(Weekday.MONDAY, d), id="calendar-now"),
    ],
)
def test_naive_datetimes_are_rejected(build) -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        build(NAIVE)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda d: booking(d, utc(2026, 7, 20, 13)), id="record-start"),
        pytest.param(
            lambda d: BookingRequest("u1", "court-1", d, utc(2026, 7, 20, 13)),
            id="request-start",
        ),
        pytest.param(lambda d: CalendarContext(Weekday.MONDAY, d), id="calendar-now"),
    ],
)
def test_non_utc_offsets_are_rejected(build) -> None:
    # An aware +02:00 datetime is an unambiguous instant, but accepting it means week-boundary
    # math in a rule silently operates on a local calendar. UTC or nothing.
    with pytest.raises(ValueError, match="must be UTC"):
        build(datetime(2026, 7, 20, 12, 0, tzinfo=PLUS_TWO))


def test_non_datetime_is_rejected_with_type_error() -> None:
    with pytest.raises(TypeError, match="must be a datetime"):
        booking("2026-07-20T12:00:00Z", utc(2026, 7, 20, 13))  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Interval validation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(utc(2026, 7, 20, 13), utc(2026, 7, 20, 12), id="reversed"),
        pytest.param(utc(2026, 7, 20, 12), utc(2026, 7, 20, 12), id="zero-length"),
    ],
)
def test_start_must_precede_end(start: datetime, end: datetime) -> None:
    with pytest.raises(ValueError, match="strictly before"):
        BookingRequest("u1", "court-1", start, end)
    with pytest.raises(ValueError, match="strictly before"):
        booking(start, end)


def test_one_microsecond_booking_is_valid() -> None:
    start = utc(2026, 7, 20, 12)
    assert BookingRequest("u1", "court-1", start, start + timedelta(microseconds=1)).duration


def test_request_exposes_duration() -> None:
    request = BookingRequest("u1", "court-1", utc(2026, 7, 20, 12), utc(2026, 7, 20, 13, 30))
    assert request.duration == timedelta(hours=1, minutes=30)


# --------------------------------------------------------------------------------------
# Frozen-ness
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instance", "attribute", "value"),
    [
        pytest.param(UserContext("u1"), "user_id", "u2", id="user"),
        pytest.param(CalendarContext(Weekday.MONDAY, NOW), "now", NOW, id="calendar"),
        pytest.param(HistoryContext(), "bookings", (), id="history"),
        pytest.param(RuleResult.allow(), "passed", False, id="result"),
        pytest.param(
            BookingRequest("u1", "c", utc(2026, 7, 20, 12), utc(2026, 7, 20, 13)),
            "start_at",
            NOW,
            id="request",
        ),
    ],
)
def test_interfaces_are_frozen(instance: object, attribute: str, value: object) -> None:
    with pytest.raises(Exception):  # FrozenInstanceError subclasses AttributeError
        setattr(instance, attribute, value)


# --------------------------------------------------------------------------------------
# UserContext — user_id only, deliberately
# --------------------------------------------------------------------------------------


def test_user_context_has_no_role_or_tier() -> None:
    user = UserContext(user_id="u1")
    assert not hasattr(user, "role")
    assert not hasattr(user, "tier")


def test_calendar_context_has_no_timezone_field() -> None:
    calendar = CalendarContext(week_starts_on=Weekday.MONDAY, now=NOW)
    assert not hasattr(calendar, "timezone")
    assert not hasattr(calendar, "tz")


def test_calendar_context_rejects_a_bare_int_weekday() -> None:
    with pytest.raises(TypeError, match="must be a Weekday"):
        CalendarContext(week_starts_on=0, now=NOW)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# LocalFrame — the local calendar, pre-answered
# --------------------------------------------------------------------------------------


def frame(**overrides: object) -> LocalFrame:
    """A valid frame for Monday 2026-07-20 on a UTC venue, with fields swapped in per test."""
    fields: dict = {
        "day_start": utc(2026, 7, 20),
        "day_end": utc(2026, 7, 21),
        "week_start": utc(2026, 7, 20),
        "week_end": utc(2026, 7, 27),
        "month_start": utc(2026, 7, 1),
        "month_end": utc(2026, 8, 1),
        "weekday": 0,
        "start_minutes": 540,
        "end_minutes": 600,
    }
    fields.update(overrides)
    return LocalFrame(**fields)


def test_a_valid_frame_round_trips() -> None:
    assert frame().weekday == 0
    assert frame().start_minutes == 540


def test_the_frame_carries_no_timezone_and_no_offset() -> None:
    """The absence *is* the invariant.

    A zone or an offset here would be readable by every rule, and a rule that can convert is a
    rule whose correctness depends on it having picked the zone the caller meant. Every local
    question is answered as a UTC instant or a plain integer instead.
    """
    built = frame()
    for forbidden in ("timezone", "tz", "tzinfo", "zone", "utc_offset", "offset"):
        assert not hasattr(built, forbidden), forbidden


@pytest.mark.parametrize(
    "field_name",
    ["day_start", "day_end", "week_start", "week_end", "month_start", "month_end"],
)
def test_every_bound_rejects_a_naive_datetime(field_name: str) -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        frame(**{field_name: NAIVE})


@pytest.mark.parametrize(
    "field_name",
    ["day_start", "day_end", "week_start", "week_end", "month_start", "month_end"],
)
def test_every_bound_rejects_a_non_zero_offset(field_name: str) -> None:
    with pytest.raises(ValueError, match="zero offset"):
        frame(**{field_name: NOW.astimezone(PLUS_TWO)})


@pytest.mark.parametrize("label", ["day", "week", "month"])
def test_an_inverted_pair_is_rejected(label: str) -> None:
    """Two absolute instants describe no recurring window, so there is no wrap to represent.

    ``AvailabilityHoursRule``'s bounds invert legitimately — they are clock times and inversion
    means "this window crosses a UTC day". These are not clock times, so an inversion here is
    exactly what it looks like: a caller that resolved the pair the wrong way round.
    """
    expected = rf"LocalFrame\.{label}_start must be strictly before LocalFrame\.{label}_end"
    with pytest.raises(ValueError, match=expected):
        frame(**{f"{label}_start": utc(2026, 8, 1), f"{label}_end": utc(2026, 7, 1)})


@pytest.mark.parametrize("label", ["day", "week", "month"])
def test_a_zero_width_pair_is_rejected(label: str) -> None:
    with pytest.raises(ValueError, match="strictly before"):
        frame(**{f"{label}_start": utc(2026, 7, 20), f"{label}_end": utc(2026, 7, 20)})


@pytest.mark.parametrize("weekday", [-1, 7, 100])
def test_weekday_outside_range_seven_is_rejected(weekday: int) -> None:
    with pytest.raises(ValueError, match="range"):
        frame(weekday=weekday)


@pytest.mark.parametrize("weekday", [0, 6])
def test_weekday_bounds_are_inclusive(weekday: int) -> None:
    assert frame(weekday=weekday).weekday == weekday


def test_weekday_rejects_a_bool() -> None:
    """``True`` is an ``int`` and would read as Tuesday."""
    with pytest.raises(TypeError, match="must be an int"):
        frame(weekday=True)


def test_negative_start_minutes_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        frame(start_minutes=-1)


def test_start_minutes_of_zero_is_a_booking_at_local_midnight() -> None:
    assert frame(start_minutes=0, end_minutes=60).start_minutes == 0


def test_end_minutes_must_be_after_start_minutes() -> None:
    with pytest.raises(ValueError, match="strictly after"):
        frame(start_minutes=600, end_minutes=600)


def test_end_minutes_may_exceed_1440() -> None:
    """A booking running past local midnight. Representing it is the point; capping it is the bug.

    ``deferred/passed-midnight.md`` is the ticket this makes expressible: 23:00–01:00 local is
    ``start_minutes=1380, end_minutes=1500``, and there is no other way to say it once the frame
    refuses to carry a zone.
    """
    built = frame(start_minutes=1380, end_minutes=1500)
    assert built.end_minutes == 1500


def test_minutes_reject_a_non_int() -> None:
    with pytest.raises(TypeError, match="must be an int"):
        frame(start_minutes=540.0)


# --------------------------------------------------------------------------------------
# HistoryContext
# --------------------------------------------------------------------------------------


def test_history_defaults_to_empty_and_coerces_to_tuple() -> None:
    assert HistoryContext().bookings == ()
    history = HistoryContext(bookings=[booking(utc(2026, 7, 20, 9), utc(2026, 7, 20, 10))])
    assert isinstance(history.bookings, tuple)
    assert len(history) == 1


def test_history_rejects_non_booking_entries() -> None:
    with pytest.raises(TypeError, match=r"bookings\[1\] must be a BookingRecord"):
        HistoryContext(bookings=(booking(utc(2026, 7, 20, 9), utc(2026, 7, 20, 10)), "nope"))


def test_booking_record_has_no_status_field() -> None:
    # Everything in HistoryContext counts; the caller filters before building it. A status field
    # here would tempt a rule into filtering against a schema this package does not own.
    record = booking(utc(2026, 7, 20, 9), utc(2026, 7, 20, 10))
    assert not hasattr(record, "status")


# --------------------------------------------------------------------------------------
# History window invariant
# --------------------------------------------------------------------------------------


def test_window_is_the_calendar_month_when_the_month_is_wider() -> None:
    lower, upper = history_window(NOW)  # mid-July
    assert lower == utc(2026, 7, 1)
    assert upper == utc(2026, 8, 1)


def test_window_extends_past_the_month_start_early_in_the_month() -> None:
    lower, upper = history_window(utc(2026, 7, 2, 12))
    assert lower == utc(2026, 7, 2, 12) - HISTORY_ROLLING_WINDOW  # late June
    assert upper == utc(2026, 8, 1)


def test_window_extends_past_the_month_end_late_in_the_month() -> None:
    lower, upper = history_window(utc(2026, 7, 30, 12))
    assert lower == utc(2026, 7, 1)
    assert upper == utc(2026, 7, 30, 12) + HISTORY_ROLLING_WINDOW  # early August


def test_window_handles_a_year_rollover() -> None:
    lower, upper = history_window(utc(2026, 12, 28, 12))
    assert lower == utc(2026, 12, 1)
    assert upper == utc(2027, 1, 4, 12)


def test_context_accepts_history_inside_the_window() -> None:
    context = make_context(booking(utc(2026, 7, 15, 9), utc(2026, 7, 15, 10)))
    assert len(context.history) == 1


@pytest.mark.parametrize(
    ("start", "end", "label"),
    [
        pytest.param(utc(2026, 6, 10, 9), utc(2026, 6, 10, 10), "before", id="last-month"),
        pytest.param(utc(2026, 8, 10, 9), utc(2026, 8, 10, 10), "after", id="next-month"),
    ],
)
def test_context_rejects_history_outside_the_window(
    start: datetime, end: datetime, label: str
) -> None:
    with pytest.raises(ValueError, match="outside the permitted history window"):
        make_context(booking(start, end))


def test_window_boundary_is_half_open() -> None:
    # A booking ending exactly at the lower bound is out; one ending a microsecond later is in.
    lower, _ = history_window(NOW)
    with pytest.raises(ValueError, match="outside the permitted history window"):
        make_context(booking(lower - timedelta(hours=1), lower))
    straddling = booking(lower - timedelta(hours=1), lower + timedelta(microseconds=1))
    assert len(make_context(straddling).history) == 1


def test_window_rejection_names_the_offending_index() -> None:
    good = booking(utc(2026, 7, 15, 9), utc(2026, 7, 15, 10))
    bad = booking(utc(2026, 6, 10, 9), utc(2026, 6, 10, 10))
    with pytest.raises(ValueError, match=r"bookings\[1\]"):
        make_context(good, bad)


def test_context_rejects_wrong_component_types() -> None:
    calendar = CalendarContext(Weekday.MONDAY, NOW)
    local = utc_frame(NOW)
    with pytest.raises(TypeError, match="must be a UserContext"):
        Context(user="u1", calendar=calendar, local=local)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a CalendarContext"):
        Context(user=UserContext("u1"), calendar=NOW, local=local)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a LocalFrame"):
        Context(user=UserContext("u1"), calendar=calendar, local=NOW)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a HistoryContext"):
        Context(  # type: ignore[arg-type]
            user=UserContext("u1"), calendar=calendar, local=local, history=()
        )


def test_context_requires_a_local_frame_with_no_default() -> None:
    """The one field on ``Context`` that must never acquire a default.

    A default could only be the UTC frame, and a rule reading that would be silently wrong for
    every venue not on UTC — the exact bug ``LocalFrame`` exists to remove, reintroduced as a
    convenience. Callers resolve a real frame or they do not get a context.
    """
    with pytest.raises(TypeError, match="local"):
        Context(  # type: ignore[call-arg]
            user=UserContext("u1"), calendar=CalendarContext(Weekday.MONDAY, NOW)
        )


def test_context_history_defaults_to_empty() -> None:
    context = Context(
        user=UserContext("u1"),
        calendar=CalendarContext(Weekday.MONDAY, NOW),
        local=utc_frame(NOW),
    )
    assert context.history.bookings == ()


def test_context_now_delegates_to_the_calendar() -> None:
    assert make_context().now == NOW


# --------------------------------------------------------------------------------------
# RuleResult
# --------------------------------------------------------------------------------------


def test_allow_and_deny_constructors() -> None:
    allowed = RuleResult.allow()
    assert allowed.passed is True
    assert allowed.fail_reason is None

    denied = RuleResult.deny("Bookings can be at most 2 hours.")
    assert denied.passed is False
    assert denied.fail_reason == "Bookings can be at most 2 hours."


def test_passing_result_must_not_carry_a_reason() -> None:
    with pytest.raises(ValueError, match="must have fail_reason=None"):
        RuleResult(passed=True, fail_reason="Bookings can be at most 2 hours.")


def test_failing_result_must_carry_a_reason() -> None:
    with pytest.raises(ValueError, match="must supply a user-facing fail_reason"):
        RuleResult(passed=False)


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_failing_result_rejects_a_blank_reason(blank: str) -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        RuleResult(passed=False, fail_reason=blank)


def test_failing_result_rejects_a_non_string_reason() -> None:
    with pytest.raises(TypeError, match="fail_reason must be a str"):
        RuleResult(passed=False, fail_reason=ValueError("boom"))  # type: ignore[arg-type]


def test_passed_must_be_a_bool_not_a_truthy_value() -> None:
    with pytest.raises(TypeError, match="passed must be a bool"):
        RuleResult(passed=1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# BaseRule
# --------------------------------------------------------------------------------------


def test_base_rule_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BaseRule()  # type: ignore[abstract]


def test_subclass_without_evaluate_cannot_be_instantiated() -> None:
    class Incomplete(BaseRule):
        pass

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_a_parameterised_rule_round_trips() -> None:
    class MaxDurationRule(BaseRule):
        def __init__(self, max_duration: timedelta) -> None:
            self.max_duration = max_duration

        def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
            if request.duration > self.max_duration:
                return RuleResult.deny("Bookings can be at most 2 hours.")
            return RuleResult.allow()

    rule = MaxDurationRule(max_duration=timedelta(hours=2))
    context = make_context()
    short = BookingRequest("u1", "court-1", utc(2026, 7, 20, 12), utc(2026, 7, 20, 13))
    long = BookingRequest("u1", "court-1", utc(2026, 7, 20, 12), utc(2026, 7, 20, 15))

    assert rule.evaluate(short, context).passed is True
    assert rule.evaluate(long, context).fail_reason == "Bookings can be at most 2 hours."
    assert rule.name == "MaxDurationRule"
