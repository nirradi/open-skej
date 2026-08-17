"""Tests for the rule engine controller.

The controller's value is in what it *doesn't* do: it doesn't run rules after a denial, it doesn't
let a buggy rule reach the caller as an exception, and it doesn't let a leaked exception string
reach the user. Each of those is asserted directly rather than inferred from a return value.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from rules.controller import RULE_ERROR_MESSAGE, ContextMismatchError, evaluate_request
from rules.interfaces import (
    BaseRule,
    BookingRecord,
    BookingRequest,
    CalendarContext,
    Context,
    HistoryContext,
    RuleResult,
    RunContext,
    UserContext,
    Weekday,
)
from tests.frames import solo_run, utc_frame

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
USER = "u1"
RESOURCE = "court-1"


def make_request(user_id: str = USER, resource_id: str = RESOURCE) -> BookingRequest:
    return BookingRequest(
        user_id=user_id,
        resource_id=resource_id,
        start_at=NOW + timedelta(hours=1),
        end_at=NOW + timedelta(hours=2),
    )


def make_context(
    *bookings: BookingRecord,
    user_id: str = USER,
    frame_for: datetime = NOW,
    run: RunContext | None = None,
) -> Context:
    return Context(
        user=UserContext(user_id=user_id),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=NOW),
        local=utc_frame(frame_for),
        run=run or solo_run(make_request()),
        history=HistoryContext(bookings=bookings),
    )


def make_booking(user_id: str = USER, resource_id: str = RESOURCE) -> BookingRecord:
    return BookingRecord(
        user_id=user_id,
        resource_id=resource_id,
        start_at=NOW - timedelta(hours=2),
        end_at=NOW - timedelta(hours=1),
    )


class SpyRule(BaseRule):
    """Records every call so a test can assert a rule was genuinely never reached."""

    def __init__(self, result: RuleResult, label: str = "spy") -> None:
        self.result = result
        self.label = label
        self.calls: list[BookingRequest] = []

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        self.calls.append(request)
        return self.result

    @property
    def called(self) -> bool:
        return bool(self.calls)


class RecordingRule(SpyRule):
    """A passing spy that also appends its label to a shared list, to pin evaluation order."""

    def __init__(self, order: list[str], label: str) -> None:
        super().__init__(RuleResult.allow(), label)
        self.order = order

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        self.order.append(self.label)
        return super().evaluate(request, context)


class ExplodingRule(BaseRule):
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or RuntimeError("secret internals: /srv/app/rules/thing.py line 42")

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult:
        raise self.exc


def test_empty_canon_passes():
    result = evaluate_request(make_request(), make_context(), [])
    assert result == RuleResult.allow()


def test_all_rules_pass_returns_allow():
    rules = [SpyRule(RuleResult.allow()) for _ in range(3)]
    result = evaluate_request(make_request(), make_context(), rules)
    assert result.passed
    assert all(rule.called for rule in rules)


def test_rules_run_in_canon_order():
    order: list[str] = []
    canon = [RecordingRule(order, label) for label in ("first", "second", "third")]
    evaluate_request(make_request(), make_context(), canon)
    assert order == ["first", "second", "third"]


def test_fail_fast_returns_the_first_denial():
    first_denial = RuleResult.deny("Only one hour at a time, please.")
    second_denial = RuleResult.deny("You've already booked twice this week.")
    canon = [
        SpyRule(RuleResult.allow()),
        SpyRule(first_denial),
        SpyRule(second_denial),
    ]
    result = evaluate_request(make_request(), make_context(), canon)
    assert result is first_denial


def test_later_rules_are_never_evaluated_after_a_denial():
    denying = SpyRule(RuleResult.deny("Nope."))
    later = SpyRule(RuleResult.allow())
    evaluate_request(make_request(), make_context(), [denying, later])
    assert denying.called
    assert not later.called, "a rule after the first denial was evaluated"


def test_raising_rule_is_contained_and_short_circuits():
    later = SpyRule(RuleResult.allow())
    result = evaluate_request(make_request(), make_context(), [ExplodingRule(), later])
    assert not result.passed
    assert not later.called


def test_raising_rule_message_is_generic_and_leaks_nothing():
    exc = RuntimeError("KeyError('resource_id') at /srv/app/rules/max_duration.py:88")
    result = evaluate_request(make_request(), make_context(), [ExplodingRule(exc)])

    assert result.fail_reason == RULE_ERROR_MESSAGE
    leaked = ["ExplodingRule", "RuntimeError", "KeyError", "Traceback", "/srv/app", ".py"]
    for fragment in leaked:
        assert fragment not in result.fail_reason


def test_raising_rule_is_logged_with_the_real_exception_and_rule_name(caplog):
    exc = RuntimeError("the actual cause")
    with caplog.at_level(logging.ERROR, logger="rules.controller"):
        evaluate_request(make_request(), make_context(), [ExplodingRule(exc)])

    record = next(r for r in caplog.records if r.levelno >= logging.ERROR)
    assert "ExplodingRule" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[1] is exc


def test_containment_is_not_specific_to_one_exception_type():
    # Anything a rule can plausibly raise — including from a bad import or a bad dataclass — is
    # contained, not just RuntimeError.
    for exc in (ValueError("bad"), TypeError("bad"), ZeroDivisionError(), AttributeError()):
        result = evaluate_request(make_request(), make_context(), [ExplodingRule(exc)])
        assert result.fail_reason == RULE_ERROR_MESSAGE


def test_rule_returning_a_non_ruleresult_is_contained():
    class BadReturnRule(BaseRule):
        def evaluate(self, request, context):
            return True  # a plausible mistake in a generated rule

    later = SpyRule(RuleResult.allow())
    result = evaluate_request(make_request(), make_context(), [BadReturnRule(), later])
    assert result.fail_reason == RULE_ERROR_MESSAGE
    assert not later.called


def test_history_for_a_different_resource_is_accepted():
    """History is filtered to the user, not the resource.

    An application scoping a frequency cap across every Resource in a Space (rather than to the one
    being booked) legitimately hands the engine one user's bookings drawn from several resources —
    the engine has no opinion on how wide that scope is, only on whose bookings it may be.
    """
    context = make_context(make_booking(resource_id="court-2"), make_booking(resource_id="court-3"))
    assert evaluate_request(make_request(), context, []).passed


def test_context_built_for_a_different_user_raises():
    """A context whose history mismatches its own `user_id` can no longer be built at all —
    `test_interfaces.py::test_context_rejects_history_for_a_different_user` covers that. What is
    still this controller's own job is catching a `Context` that is internally consistent but was
    simply built for the wrong person.
    """
    context = make_context(user_id="other-user")
    with pytest.raises(ContextMismatchError, match="other-user"):
        evaluate_request(make_request(), context, [])


def test_mismatch_is_checked_before_any_rule_runs():
    spy = SpyRule(RuleResult.allow())
    context = make_context(user_id="someone-else")
    with pytest.raises(ContextMismatchError):
        evaluate_request(make_request(), context, [spy])
    assert not spy.called


def test_matching_history_is_accepted():
    context = make_context(make_booking(), make_booking())
    assert evaluate_request(make_request(), context, []).passed


def test_a_frame_resolved_for_the_wrong_date_raises():
    """Fail closed on the outcome, loud on the cause — the same split the user check draws.

    A frame resolved for another date has a day, a week and a month all describing a different
    stretch of the calendar than the booking sits in, so a rule counting "bookings in this local
    day" would quietly count another day's. Denying would present that adapter bug as an ordinary
    refusal, which is exactly what would keep it from being found.
    """
    context = make_context(frame_for=NOW + timedelta(days=1))
    with pytest.raises(ContextMismatchError, match="local day"):
        evaluate_request(make_request(), context, [])


def test_the_frame_check_runs_before_any_rule():
    spy = SpyRule(RuleResult.allow())
    context = make_context(frame_for=NOW - timedelta(days=1))
    with pytest.raises(ContextMismatchError):
        evaluate_request(make_request(), context, [spy])
    assert not spy.called


def test_a_booking_starting_exactly_at_the_day_boundary_is_in_frame():
    """``[day_start, day_end)`` is half-open, so local midnight belongs to the day it opens."""
    midnight = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
    request = BookingRequest(USER, RESOURCE, midnight, midnight + timedelta(hours=1))
    context = Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=NOW),
        local=utc_frame(midnight),
        run=solo_run(request),
        history=HistoryContext(),
    )
    assert evaluate_request(request, context, []).passed


def test_a_booking_running_past_the_local_day_is_accepted():
    """Only ``start_at`` is checked. A booking ending past local midnight is the case
    ``end_minutes > 1440`` exists to represent, and refusing it here would make the frame
    unable to describe the thing it was added to describe."""
    late = datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc)
    request = BookingRequest(USER, RESOURCE, late, late + timedelta(hours=2))
    context = Context(
        user=UserContext(user_id=USER),
        calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=NOW),
        local=utc_frame(late, late + timedelta(hours=2)),
        run=solo_run(request),
        history=HistoryContext(),
    )
    assert context.local.end_minutes == 1500
    assert evaluate_request(request, context, []).passed


def test_canon_may_be_any_iterable():
    rules = (SpyRule(RuleResult.allow()), SpyRule(RuleResult.deny("no")))
    result = evaluate_request(make_request(), make_context(), iter(rules))
    assert not result.passed


# --------------------------------------------------------------------------------------
# The run cross-check
# --------------------------------------------------------------------------------------
#
# Same reasoning as the local-frame check above: a run that does not contain its own request
# describes a different stretch of the calendar than the booking sits in, so a rule reading
# ``run.duration`` would judge someone else's session. Fail closed on the outcome, loud on the
# cause — this must raise, not deny.


def test_a_run_starting_after_the_request_raises():
    """The run's own start is later than the request it supposedly contains — this booking is not
    actually inside the span the adapter resolved."""
    request = make_request()
    run = RunContext(
        start_at=request.start_at + timedelta(minutes=1), end_at=request.end_at, booking_count=1
    )
    context = make_context(run=run)
    with pytest.raises(ContextMismatchError, match="does not contain its own request"):
        evaluate_request(request, context, [])


def test_a_run_ending_before_the_request_raises():
    """The mirror case: the run's own end falls before the request's end."""
    request = make_request()
    run = RunContext(
        start_at=request.start_at, end_at=request.end_at - timedelta(minutes=1), booking_count=1
    )
    context = make_context(run=run)
    with pytest.raises(ContextMismatchError, match="does not contain its own request"):
        evaluate_request(request, context, [])


def test_the_run_check_runs_before_any_rule():
    spy = SpyRule(RuleResult.allow())
    request = make_request()
    run = RunContext(
        start_at=request.start_at, end_at=request.end_at - timedelta(minutes=1), booking_count=1
    )
    context = make_context(run=run)
    with pytest.raises(ContextMismatchError):
        evaluate_request(request, context, [spy])
    assert not spy.called


def test_a_run_wider_than_the_request_on_both_sides_is_accepted():
    """The run may legitimately be bigger than the request — it is the whole merged span, and the
    request may be extending one end of a run that already reaches past the other."""
    request = make_request()
    run = RunContext(
        start_at=request.start_at - timedelta(hours=1),
        end_at=request.end_at + timedelta(hours=1),
        booking_count=3,
    )
    context = make_context(run=run)
    assert evaluate_request(request, context, []).passed


def test_a_run_exactly_equal_to_the_request_is_accepted():
    """The common case: no adjoining bookings, so the run is exactly the request, count 1."""
    request = make_request()
    context = make_context(run=solo_run(request))
    assert evaluate_request(request, context, []).passed
