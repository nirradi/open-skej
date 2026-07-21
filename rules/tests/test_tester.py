"""Tests for Agent B and for shipping the engine into the sandbox.

**No test here calls a model or runs the ``claude`` binary.** The client is a fake returning canned
text. Some tests do run the real sandbox — that is a local subprocess, not a network call, and it is
the only way to establish the thing this module most needs to establish: that a candidate written
the way the Generator is told to write one actually loads and runs in there.
"""

import textwrap

import pytest

from generation.errors import GenerationError, SuiteRejectedError
from generation.harness import (
    ENGINE_NAMES,
    PRELUDE,
    assemble_candidate_module,
    engine_source,
    run_candidate,
    sandbox_files,
)
from generation.tester import SYSTEM_PROMPT, build_test_prompt, generate_tests
from rules.safety import UnsafeRuleError, validate_source
from rules.sandbox import SandboxOutcome

# --------------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------------

#: A candidate in exactly the shape the Generator's system prompt demands: engine types as free
#: names, ``timedelta`` imported explicitly, parameters on the instance.
CANDIDATE = textwrap.dedent('''\
    from datetime import timedelta


    class MaxDurationRule(BaseRule):
        """Bookings may not run longer than ``max_duration``. The bound is inclusive."""

        def __init__(self, max_duration):
            if max_duration <= timedelta(0):
                raise ValueError(f"max_duration must be positive; got {max_duration!r}")
            self.max_duration = max_duration

        def evaluate(self, request, context):
            if request.duration > self.max_duration:
                return RuleResult.deny("Bookings can be at most 2 hours long.")
            return RuleResult.allow()
    ''')

#: A suite written the way the Tester's prompt describes: imports the candidate by name, imports
#: the engine types from ``engine``, fixed literal UTC datetimes, and a fail-closed probe.
CANDIDATE_TESTS = textwrap.dedent("""\
    from datetime import datetime, timedelta, timezone

    import pytest

    from candidate_rule import MaxDurationRule
    from engine import (
        BookingRecord,
        BookingRequest,
        CalendarContext,
        Context,
        HistoryContext,
        RuleResult,
        UserContext,
        Weekday,
    )

    NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    LIMIT = timedelta(hours=2)


    def context(bookings=()):
        return Context(
            user=UserContext("user-1"),
            calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=NOW),
            history=HistoryContext(bookings=tuple(bookings)),
        )


    def request(hours):
        return BookingRequest("user-1", "court-1", NOW, NOW + timedelta(hours=hours))


    def test_short_booking_is_allowed():
        assert MaxDurationRule(LIMIT).evaluate(request(1), context()).passed


    def test_exact_limit_is_allowed():
        assert MaxDurationRule(LIMIT).evaluate(request(2), context()).passed


    def test_one_second_over_the_limit_is_refused():
        req = BookingRequest("user-1", "court-1", NOW, NOW + LIMIT + timedelta(seconds=1))
        result = MaxDurationRule(LIMIT).evaluate(req, context())
        assert not result.passed
        assert result.fail_reason


    def test_returns_a_rule_result():
        assert isinstance(MaxDurationRule(LIMIT).evaluate(request(1), context()), RuleResult)


    def test_rejects_a_nonsensical_limit():
        with pytest.raises(ValueError):
            MaxDurationRule(timedelta(0))


    def test_history_of_another_user_does_not_make_it_pass():
        other = BookingRecord("user-2", "court-1", NOW, NOW + timedelta(hours=9))
        result = MaxDurationRule(LIMIT).evaluate(request(9), context([other]))
        assert not result.passed


    def test_fails_closed_on_input_it_cannot_evaluate():
        rule = MaxDurationRule(LIMIT)
        try:
            result = rule.evaluate(object(), context())
        except Exception:
            return
        assert not result.passed, "a rule that cannot decide must not allow the booking"
    """)


class FakeClient:
    """An ``LLMClient`` returning canned text and recording every call made to it."""

    def __init__(self, text=CANDIDATE_TESTS):
        self.text = text
        self.calls = []

    def complete(self, *, system, prompt, model="stub-model"):
        self.calls.append({"system": system, "prompt": prompt, "model": model})
        from generation.llm import LLMResponse

        return LLMResponse(text=self.text, model=model)


# --------------------------------------------------------------------------------------------
# The integration the whole loop rests on: a free-name candidate running in the sandbox
# --------------------------------------------------------------------------------------------


def test_candidate_without_the_prelude_cannot_even_be_imported():
    """The problem the harness exists to solve, pinned so it cannot be silently reintroduced.

    A candidate uses ``BaseRule`` as a free name because the safety validator forbids importing it.
    The sandbox binds nothing and gives the child no ``PYTHONPATH``, so run as-is the candidate dies
    at class-definition time. It surfaces as ``CRASHED``, which a loop would spend its whole retry
    budget on while reporting a well-behaved give-up.
    """
    from rules.sandbox import run_tests

    result = run_tests(CANDIDATE, CANDIDATE_TESTS)

    assert result.outcome is SandboxOutcome.CRASHED
    assert "BaseRule" in result.stdout + result.stderr


def test_candidate_runs_in_the_sandbox_with_the_engine_shipped_in():
    """End to end, for real: assembled candidate + engine.py + adversarial suite, all passing."""
    result = run_candidate(CANDIDATE, CANDIDATE_TESTS)

    assert result.outcome is SandboxOutcome.PASSED, result.stdout + result.stderr
    assert result.passed


def test_a_wrong_candidate_fails_rather_than_crashing():
    """The suite must be able to return a *verdict*, not just load. FAILED, not CRASHED."""
    broken = CANDIDATE.replace("request.duration > self.max_duration", "False")

    result = run_candidate(broken, CANDIDATE_TESTS)

    assert result.outcome is SandboxOutcome.FAILED


def test_prelude_binds_exactly_the_names_the_generator_promises():
    """The prompt says six free names; the prelude is what makes that true, so they must agree."""
    for name in ENGINE_NAMES:
        assert f"    {name},\n" in PRELUDE
    assert PRELUDE.count(",\n") == len(ENGINE_NAMES)


def test_engine_source_is_the_real_interfaces_module():
    source = engine_source()

    assert "class BaseRule" in source
    assert "class RuleResult" in source
    assert sandbox_files()["engine.py"] == source


def test_the_prelude_would_not_survive_the_validator_it_is_prepended_after():
    """Why the ordering in ``harness`` is load-bearing, asserted rather than only commented.

    ``validate_source`` runs on the generated source alone. The assembled module imports ``engine``,
    which is not on the import allowlist — so a later editor who "tidies" this by validating the
    assembled module turns every candidate into a rejection.
    """
    validate_source(CANDIDATE)  # the generated source alone is fine

    with pytest.raises(UnsafeRuleError, match="engine"):
        validate_source(assemble_candidate_module(CANDIDATE))


def test_assembled_module_keeps_the_candidate_verbatim():
    assembled = assemble_candidate_module(CANDIDATE)

    assert assembled.endswith(CANDIDATE)
    assert assembled.startswith(PRELUDE)


# --------------------------------------------------------------------------------------------
# generate_tests
# --------------------------------------------------------------------------------------------


def test_generate_tests_returns_the_models_source():
    client = FakeClient()

    tests = generate_tests(CANDIDATE, "bookings can be at most 2 hours", client=client)

    assert tests == CANDIDATE_TESTS.strip()


def test_generate_tests_strips_a_markdown_fence():
    client = FakeClient(f"Here you go:\n\n```python\n{CANDIDATE_TESTS}```\n")

    tests = generate_tests(CANDIDATE, "max 2 hours", client=client)

    assert tests.startswith("from datetime import")
    assert "```" not in tests


def test_the_prompt_carries_both_the_constraint_and_the_candidate():
    client = FakeClient()

    generate_tests(CANDIDATE, "bookings can be at most 2 hours", client=client)

    prompt = client.calls[0]["prompt"]
    assert "bookings can be at most 2 hours" in prompt
    assert "class MaxDurationRule(BaseRule):" in prompt


def test_prose_instead_of_code_is_rejected():
    client = FakeClient("I would test this by checking the duration boundary carefully.")

    with pytest.raises(SuiteRejectedError, match="does not parse"):
        generate_tests(CANDIDATE, "max 2 hours", client=client)


def test_an_empty_answer_is_rejected():
    client = FakeClient("   \n  ")

    with pytest.raises(SuiteRejectedError, match="no Python source"):
        generate_tests(CANDIDATE, "max 2 hours", client=client)


def test_a_module_with_no_test_function_is_rejected():
    """pytest would exit 5 and the sandbox would call it a crash. Better to say what was missing."""
    client = FakeClient("from candidate_rule import MaxDurationRule\n\nRULE = MaxDurationRule\n")

    with pytest.raises(SuiteRejectedError, match="no test function"):
        generate_tests(CANDIDATE, "max 2 hours", client=client)


def test_tests_rejected_is_a_generation_error_and_carries_the_source():
    client = FakeClient("not python at all !!!")

    with pytest.raises(GenerationError) as caught:
        generate_tests(CANDIDATE, "max 2 hours", client=client)

    assert isinstance(caught.value, SuiteRejectedError)
    assert caught.value.source == "not python at all !!!"


def test_test_source_is_not_put_through_the_rule_validator():
    """A deliberate decision, pinned: real test code needs constructs a rule may never have.

    ``import pytest`` alone fails the rule validator's import allowlist. If a later change routed
    test source through ``validate_source``, every useful suite would be rejected — so the fact
    that a good suite is *not* validator-clean is asserted here rather than left to be rediscovered.
    """
    with pytest.raises(UnsafeRuleError):
        validate_source(CANDIDATE_TESTS)

    client = FakeClient()
    assert generate_tests(CANDIDATE, "max 2 hours", client=client)  # accepted regardless


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_inputs_are_a_caller_error(bad):
    client = FakeClient()

    with pytest.raises(ValueError):
        generate_tests(bad, "max 2 hours", client=client)
    with pytest.raises(ValueError):
        generate_tests(CANDIDATE, bad, client=client)

    assert client.calls == []


# --------------------------------------------------------------------------------------------
# The system prompt states what the loop depends on
# --------------------------------------------------------------------------------------------


def test_system_prompt_names_the_modules_the_sandbox_actually_writes():
    assert "candidate_rule" in SYSTEM_PROMPT
    assert "from engine import" in SYSTEM_PROMPT


@pytest.mark.parametrize(
    "demand",
    [
        "FAIL-CLOSED PROBE",
        "(n+1)th",
        "half-open",
        "year rollover",
        "datetime.now()",
        "POSITIVE CASES",
    ],
)
def test_system_prompt_demands_the_adversarial_cases(demand):
    assert demand in SYSTEM_PROMPT


def test_build_test_prompt_frames_the_constraint_as_intent_not_behaviour():
    prompt = build_test_prompt(CANDIDATE, "max 2 hours")

    assert "supposed to enforce" in prompt
