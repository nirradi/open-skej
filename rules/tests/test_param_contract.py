"""Tests for ``generation.param_contract`` — the check that a rule's parameters are the values it
will actually be handed.

Two halves, and the second matters as much as the first. The positive cases are the reported
defect: source that treats an integer parameter as a ``timedelta`` and would take a whole Space off
line the moment an admin added it. The negative cases are the guard on the guard — a false positive
here rejects a rule the model got right, spends a retry, and at three retries fails a generation
job outright, so every shape a correct rule legitimately uses is pinned as passing.

The two ``REPORTED_*`` fixtures are reconstructions of the two rules that actually did this in the
sandbox, per ``ops/pending/bugs/generated-rule-duration-params-deny-every-booking.md``. They are
kept in the shape they were reported in rather than reduced to a minimal case: a check that only
catches the minimal form of a defect is a check that stops catching it the moment a model phrases
it slightly differently.
"""

import textwrap

import pytest

from generation.errors import RuleContractError, RuleRejectedError
from generation.generator import SYSTEM_PROMPT, generate_rule
from generation.llm import LLMResponse
from generation.param_contract import (
    CONTRACT_SUMMARY,
    constructor_params,
    describe_param_contract_findings,
    param_contract_findings,
)
from generation.stub import StubLLMClient

# --------------------------------------------------------------------------------------------
# The two rules that actually did this
# --------------------------------------------------------------------------------------------

#: `space_rules` row 12, `params={'cool_down_duration': 30}`, which died with
#: `TypeError("'<' not supported between instances of 'int' and 'datetime.timedelta'")`.
REPORTED_COOL_DOWN = textwrap.dedent('''\
    from datetime import timedelta


    class RequiredCoolDownRule(BaseRule):
        """A booking must leave ``cool_down_duration`` between it and the previous one."""

        def __init__(self, cool_down_duration):
            if cool_down_duration < timedelta(0):
                raise ValueError("cool_down_duration must not be negative")
            self.cool_down_duration = cool_down_duration

        def evaluate(self, request, context):
            for booking in context.history.bookings:
                if request.start_at - booking.end_at < self.cool_down_duration:
                    return RuleResult.deny("Please leave a gap between bookings.")
            return RuleResult.allow()
    ''')

#: `space_rules` row 18, `params={'max_weekly_duration': 360}`, which died with
#: `TypeError("'<=' not supported between instances of 'int' and 'datetime.timedelta'")`.
REPORTED_WEEKLY_TOTAL = textwrap.dedent('''\
    from datetime import timedelta


    class MaximumWeeklyInstrumentTimeRule(BaseRule):
        """A member's total booked time in the local week may not exceed ``max_weekly_duration``."""

        def __init__(self, max_weekly_duration):
            if max_weekly_duration <= timedelta(0):
                raise ValueError("max_weekly_duration must be positive")
            self.max_weekly_duration = max_weekly_duration

        def evaluate(self, request, context):
            total = request.duration
            for booking in context.history.bookings:
                if context.local.week_start <= booking.start_at < context.local.week_end:
                    total = total + (booking.end_at - booking.start_at)
            if total > self.max_weekly_duration:
                return RuleResult.deny("That is more instrument time than the weekly cap allows.")
            return RuleResult.allow()
    ''')


# --------------------------------------------------------------------------------------------
# Source that breaks the contract
# --------------------------------------------------------------------------------------------

STORES_THE_RAW_PARAMETER = textwrap.dedent('''\
    from datetime import timedelta


    class MaxDurationRule(BaseRule):
        """Validated correctly and stored wrong: the mistake only surfaces in ``evaluate``."""

        def __init__(self, max_duration):
            if max_duration <= 0:
                raise ValueError("max_duration must be positive")
            self.max_duration = max_duration

        def evaluate(self, request, context):
            if request.duration > self.max_duration:
                return RuleResult.deny("Too long.")
            return RuleResult.allow()
    ''')

READS_A_TIMEDELTA_ATTRIBUTE = textwrap.dedent("""\
    class MaxDurationRule(BaseRule):
        def __init__(self, max_duration):
            self.max_duration = max_duration

        def evaluate(self, request, context):
            limit_minutes = int(self.max_duration.total_seconds() // 60)
            if request.duration > request.duration:
                return RuleResult.deny(f"At most {limit_minutes} minutes.")
            return RuleResult.allow()
    """)

TIMEDELTA_DEFAULT = textwrap.dedent("""\
    from datetime import timedelta


    class RollingWindowRule(BaseRule):
        def __init__(self, max_bookings, window=timedelta(days=7)):
            self.max_bookings = max_bookings
            self.window = window

        def evaluate(self, request, context):
            return RuleResult.allow()
    """)

COMPARES_AGAINST_THE_RUN = textwrap.dedent("""\
    class MaxConsecutiveRule(BaseRule):
        def __init__(self, max_run):
            self.max_run = max_run

        def evaluate(self, request, context):
            if context.run.duration > self.max_run:
                return RuleResult.deny("Too much back to back.")
            return RuleResult.allow()
    """)

#: The `evaluate` half of `REPORTED_WEEKLY_TOTAL`, with its `__init__` corrected.
#:
#: `REPORTED_WEEKLY_TOTAL` breaks the contract twice — once in `__init__` (`<= timedelta(0)`) and
#: once in `evaluate` (against a local accumulating `timedelta`s) — so it is caught even by a check
#: that can only read the constructor, and on its own it proves nothing about the second half. This
#: is that second half alone, and it is the shape row 18 actually died on: the `TypeError` the bug
#: reports names `'<='`, which is the comparison below and not the one in the constructor.
TOTALS_INTO_A_LOCAL = textwrap.dedent("""\
    from datetime import timedelta


    class MaximumWeeklyInstrumentTimeRule(BaseRule):
        def __init__(self, max_weekly_duration):
            if max_weekly_duration <= 0:
                raise ValueError("max_weekly_duration must be positive")
            self.max_weekly_duration = max_weekly_duration

        def evaluate(self, request, context):
            total = timedelta()
            for booking in context.history.bookings:
                total += booking.end_at - booking.start_at
            if self.max_weekly_duration <= total:
                return RuleResult.deny("That is more instrument time than the weekly cap allows.")
            return RuleResult.allow()
    """)

#: The same trap in the smallest shape that still shows it: one local, one comparison.
COMPARES_AGAINST_A_DURATION_LOCAL = textwrap.dedent("""\
    class MaxDurationRule(BaseRule):
        def __init__(self, max_duration):
            if max_duration <= 0:
                raise ValueError("max_duration must be positive")
            self.max_duration = max_duration

        def evaluate(self, request, context):
            requested = request.duration
            if requested > self.max_duration:
                return RuleResult.deny("Too long.")
            return RuleResult.allow()
    """)

BREAKS_THE_CONTRACT = {
    "reported_cool_down": REPORTED_COOL_DOWN,
    "reported_weekly_total": REPORTED_WEEKLY_TOTAL,
    "totals_into_a_local": TOTALS_INTO_A_LOCAL,
    "compares_against_a_duration_local": COMPARES_AGAINST_A_DURATION_LOCAL,
    "stores_the_raw_parameter": STORES_THE_RAW_PARAMETER,
    "reads_a_timedelta_attribute": READS_A_TIMEDELTA_ATTRIBUTE,
    "timedelta_default": TIMEDELTA_DEFAULT,
    "compares_against_the_run": COMPARES_AGAINST_THE_RUN,
}


# --------------------------------------------------------------------------------------------
# Source that honours it — the false-positive guard
# --------------------------------------------------------------------------------------------

CONVERTS_IN_INIT = textwrap.dedent('''\
    from datetime import timedelta


    class MaxDurationRule(BaseRule):
        """The shape the Generator's prompt now teaches, and the shape the stub client returns."""

        def __init__(self, max_duration_minutes):
            if max_duration_minutes <= 0:
                raise ValueError("max_duration_minutes must be positive")
            self.max_duration_minutes = max_duration_minutes
            self.max_duration = timedelta(minutes=max_duration_minutes)

        def evaluate(self, request, context):
            if request.duration > self.max_duration:
                return RuleResult.deny(f"At most {self.max_duration_minutes} minutes.")
            return RuleResult.allow()
    ''')

CONVERTS_AT_THE_COMPARISON = textwrap.dedent('''\
    from datetime import timedelta


    class MaxDurationRule(BaseRule):
        """Keeps the integer and converts where it is compared. Also correct."""

        def __init__(self, max_duration_minutes):
            self.max_duration_minutes = max_duration_minutes

        def evaluate(self, request, context):
            if request.duration > timedelta(minutes=self.max_duration_minutes):
                return RuleResult.deny("Too long.")
            return RuleResult.allow()
    ''')

COUNTS_BOOKINGS = textwrap.dedent('''\
    class MaxBookingsPerDayRule(BaseRule):
        """A count parameter — the shape that was already working, and must keep working."""

        def __init__(self, max_bookings):
            if max_bookings < 1:
                raise ValueError("max_bookings must be at least 1")
            self.max_bookings = max_bookings

        def evaluate(self, request, context):
            count = 1
            for booking in context.history.bookings:
                if context.local.day_start <= booking.start_at < context.local.day_end:
                    count = count + 1
            if count > self.max_bookings:
                return RuleResult.deny("Too many bookings today.")
            return RuleResult.allow()
    ''')

LOCAL_TIME_MINUTES = textwrap.dedent('''\
    class NotBeforeRule(BaseRule):
        """A `local_time` parameter: minutes from local midnight, compared against minutes."""

        def __init__(self, earliest_minutes):
            if earliest_minutes < 0 or earliest_minutes >= 1440:
                raise ValueError("earliest_minutes must be within a day")
            self.earliest_minutes = earliest_minutes

        def evaluate(self, request, context):
            if context.local.start_minutes < self.earliest_minutes:
                return RuleResult.deny("Earlier than this space opens.")
            return RuleResult.allow()
    ''')

MINUTES_ARITHMETIC = textwrap.dedent('''\
    class MaxDurationRule(BaseRule):
        """Converts the other way — the span into minutes — which is equally correct."""

        def __init__(self, max_duration_minutes):
            self.max_duration_minutes = max_duration_minutes

        def evaluate(self, request, context):
            booked_minutes = request.duration.total_seconds() / 60
            if booked_minutes > self.max_duration_minutes:
                return RuleResult.deny("Too long.")
            return RuleResult.allow()
    ''')

NO_PARAMETERS_AT_ALL = textwrap.dedent("""\
    class NoWeekendBookingsRule(BaseRule):
        def evaluate(self, request, context):
            if context.local.weekday >= 5:
                return RuleResult.deny("No bookings at the weekend.")
            return RuleResult.allow()
    """)

#: A local holding an integer count of minutes, compared against the parameter. The guard against
#: the local tracking above becoming "any local is a duration": `used_minutes` and `total` are both
#: locals compared against a parameter, and only one of them is a mistake.
COUNTS_MINUTES_IN_A_LOCAL = textwrap.dedent("""\
    class MaxWeeklyMinutesRule(BaseRule):
        def __init__(self, max_weekly_minutes):
            self.max_weekly_minutes = max_weekly_minutes

        def evaluate(self, request, context):
            used_minutes = 0
            for booking in context.history.bookings:
                used_minutes += int((booking.end_at - booking.start_at).total_seconds() // 60)
            if used_minutes > self.max_weekly_minutes:
                return RuleResult.deny("Over the weekly cap.")
            return RuleResult.allow()
    """)

#: A local that is a duration, converted at the comparison rather than in ``__init__``. Legal, and
#: the shape a check keying on "parameter appears near a duration-valued local" would flag.
CONVERTS_A_LOCAL_AT_THE_COMPARISON = textwrap.dedent("""\
    from datetime import timedelta


    class MaxDurationRule(BaseRule):
        def __init__(self, max_duration_minutes):
            self.max_duration_minutes = max_duration_minutes

        def evaluate(self, request, context):
            requested = request.duration
            if requested > timedelta(minutes=self.max_duration_minutes):
                return RuleResult.deny("Too long.")
            return RuleResult.allow()
    """)

HONOURS_THE_CONTRACT = {
    "converts_in_init": CONVERTS_IN_INIT,
    "converts_at_the_comparison": CONVERTS_AT_THE_COMPARISON,
    "counts_bookings": COUNTS_BOOKINGS,
    "counts_minutes_in_a_local": COUNTS_MINUTES_IN_A_LOCAL,
    "converts_a_local_at_the_comparison": CONVERTS_A_LOCAL_AT_THE_COMPARISON,
    "local_time_minutes": LOCAL_TIME_MINUTES,
    "minutes_arithmetic": MINUTES_ARITHMETIC,
    "no_parameters_at_all": NO_PARAMETERS_AT_ALL,
}


# --------------------------------------------------------------------------------------------
# param_contract_findings
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", BREAKS_THE_CONTRACT.values(), ids=BREAKS_THE_CONTRACT.keys())
def test_source_that_treats_a_parameter_as_a_timedelta_is_reported(source):
    assert param_contract_findings(source)


@pytest.mark.parametrize("source", HONOURS_THE_CONTRACT.values(), ids=HONOURS_THE_CONTRACT.keys())
def test_source_that_honours_the_contract_is_not_reported(source):
    assert param_contract_findings(source) == ()


def test_the_reported_weekly_total_is_caught_in_evaluate_and_not_only_in_init():
    """The half of row 18 that a constructor-only check cannot see.

    ``REPORTED_WEEKLY_TOTAL`` violates the contract in both of its methods, so its presence in
    ``BREAKS_THE_CONTRACT`` is satisfied by the ``__init__`` violation alone and says nothing about
    the comparison that actually raised. The ``TypeError`` the bug records names ``'<='`` — the
    comparison in ``evaluate``, against a local the loop accumulated ``timedelta``s into. Asserting
    on the corrected-constructor variant is what keeps that coverage from regressing silently.
    """
    findings = param_contract_findings(TOTALS_INTO_A_LOCAL)
    assert findings
    assert "max_weekly_duration" in findings[0]


def test_a_local_is_only_a_duration_where_its_assignment_says_so():
    """Locals are classified from what is assigned to them, so the two shapes below — a parameter
    compared against a local in both — are told apart rather than flagged together."""
    assert param_contract_findings(COMPARES_AGAINST_A_DURATION_LOCAL)
    assert param_contract_findings(COUNTS_MINUTES_IN_A_LOCAL) == ()


def test_a_local_assigned_two_different_kinds_is_not_judged():
    """Conservative in the same way ``_integer_self_attrs`` is: a name this pass cannot resolve to
    one kind is dropped, because a false positive costs the model a retry it did not need."""
    source = textwrap.dedent("""\
        class R(BaseRule):
            def __init__(self, limit):
                self.limit = limit

            def evaluate(self, request, context):
                total = request.duration
                total = 0
                if total > self.limit:
                    return RuleResult.deny("no")
                return RuleResult.allow()
        """)
    assert param_contract_findings(source) == ()


def test_a_finding_names_the_parameter_and_the_line():
    """The finding is fed straight back to the model, so it has to say which argument and where.

    A message saying only "a parameter is wrong" sends the next attempt to guess, and the retry
    budget is three.
    """
    findings = param_contract_findings(REPORTED_COOL_DOWN)
    assert all("cool_down_duration" in finding for finding in findings)
    # Both places it goes wrong, in source order: the validation in `__init__` that compares the
    # raw integer against `timedelta(0)`, and the comparison in `evaluate` against the difference
    # of two datetimes. Reporting only the first would have the model fix that line and hand back
    # a rule that still raises, one attempt later, out of its retry budget.
    assert [finding.split()[1] for finding in findings] == ["8", "14"]


def test_the_stub_clients_own_rule_honours_the_contract():
    """``StubLLMClient``'s canned rule is the worked example the whole test suite and the E2E run
    build on. If it ever stopped honouring the contract, every one of those would be exercising a
    shape the real gate rejects."""
    stub_source = StubLLMClient().complete(system=SYSTEM_PROMPT, prompt="anything").text
    assert param_contract_findings(stub_source) == ()


def test_unparseable_source_reports_nothing():
    """``validate_source`` already rejects source that does not parse, with a message naming the
    line. Reporting the same failure a second time in a different vocabulary would send the model
    two accounts of one problem."""
    assert param_contract_findings("class Rule(BaseRule)\n    oops") == ()


def test_source_with_no_single_obvious_rule_class_reports_nothing():
    two_classes = (
        "class OneRule(BaseRule):\n"
        "    def __init__(self, a):\n"
        "        self.a = a\n"
        "\n"
        "class TwoRule(BaseRule):\n"
        "    def __init__(self, b):\n"
        "        self.b = b\n"
    )
    assert param_contract_findings(two_classes) == ()


def test_the_feedback_states_the_contract_before_the_findings():
    message = describe_param_contract_findings(param_contract_findings(REPORTED_WEEKLY_TOTAL))
    assert CONTRACT_SUMMARY in message
    assert message.index(CONTRACT_SUMMARY) < message.index("max_weekly_duration")


# --------------------------------------------------------------------------------------------
# constructor_params
# --------------------------------------------------------------------------------------------


def test_constructor_params_reports_the_arguments_in_order_without_self():
    assert constructor_params(TIMEDELTA_DEFAULT) == ("max_bookings", "window")


def test_constructor_params_is_empty_for_a_rule_that_takes_none():
    assert constructor_params(NO_PARAMETERS_AT_ALL) == ()


def test_constructor_params_is_empty_for_unreadable_source():
    assert constructor_params("not python at all ((") == ()


# --------------------------------------------------------------------------------------------
# generate_rule refuses a candidate that breaks the contract
# --------------------------------------------------------------------------------------------


class _CannedClient:
    default_model = "canned"

    def __init__(self, text):
        self.text = text

    def complete(self, *, system, prompt, model=None):
        return LLMResponse(text=self.text, model=model or self.default_model)


@pytest.mark.parametrize("source", BREAKS_THE_CONTRACT.values(), ids=BREAKS_THE_CONTRACT.keys())
def test_generate_rule_refuses_a_candidate_that_breaks_the_contract(source):
    with pytest.raises(RuleContractError) as excinfo:
        generate_rule("max 1 hour", client=_CannedClient(source))
    assert excinfo.value.source == source.strip()


def test_a_contract_rejection_is_a_rule_rejection_so_the_loop_retries_it():
    """``RuleContractError`` subclasses ``RuleRejectedError`` deliberately: every caller that
    already handles a rejected candidate must handle this one, or the defect walks past."""
    with pytest.raises(RuleRejectedError):
        generate_rule("max 1 hour", client=_CannedClient(REPORTED_COOL_DOWN))


def test_generate_rule_accepts_a_candidate_that_honours_the_contract():
    source = generate_rule("max 1 hour", client=_CannedClient(CONVERTS_IN_INIT))
    assert source == CONVERTS_IN_INIT.strip()
