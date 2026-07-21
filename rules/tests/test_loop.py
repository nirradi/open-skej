"""Tests for the retry loop.

**Both agents are mocked and no test makes a live call or spawns the ``claude`` binary.** The fake
client is scripted turn by turn: the loop alternates Generator and Tester calls against one client,
so a script is a list of what each successive call returns.

The sandbox, by contrast, is real in most of these. It is a local subprocess, and faking it would
mean the loop's central claim — that only a genuinely passing suite advances a candidate — was
asserted against a stub that says so. Where a specific outcome is hard to produce honestly (a
timeout costs wall-clock seconds), the sandbox is patched, and that is called out.
"""

import textwrap

import pytest

from generation import loop as loop_module
from generation.errors import LLMCallError
from generation.generator import SYSTEM_PROMPT as GENERATOR_SYSTEM_PROMPT
from generation.llm import LLMResponse
from generation.loop import (
    MAX_RETRIES,
    AttemptOutcome,
    describe_failure,
    run_generation_loop,
    write_artifact,
)
from generation.tester import SYSTEM_PROMPT as TESTER_SYSTEM_PROMPT
from rules.sandbox import SandboxOutcome, SandboxResult

# --------------------------------------------------------------------------------------------
# Candidates the fake Generator can return
# --------------------------------------------------------------------------------------------

GOOD_RULE = textwrap.dedent('''\
    from datetime import timedelta


    class MaxDurationRule(BaseRule):
        """Bookings may not run longer than ``max_duration``. Inclusive bound."""

        def __init__(self, max_duration):
            if max_duration <= timedelta(0):
                raise ValueError("max_duration must be positive")
            self.max_duration = max_duration

        def evaluate(self, request, context):
            if request.duration > self.max_duration:
                return RuleResult.deny("Bookings can be at most 2 hours long.")
            return RuleResult.allow()
    ''')

#: Wrong in the way rules are actually wrong: the bound is off by one, so the suite gets a verdict.
WRONG_RULE = GOOD_RULE.replace(
    "if request.duration > self.max_duration:",
    "if request.duration > self.max_duration + timedelta(hours=1):",
)

#: Rejected by the safety validator before anything runs — it imports the engine.
UNSAFE_RULE = "from rules.interfaces import BaseRule\n\n\nclass R(BaseRule):\n    pass\n"

TESTS = textwrap.dedent("""\
    from datetime import datetime, timedelta, timezone

    from candidate_rule import MaxDurationRule
    from engine import BookingRequest, CalendarContext, Context, RuleResult, UserContext, Weekday

    NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    LIMIT = timedelta(hours=2)


    def context():
        return Context(
            user=UserContext("user-1"),
            calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=NOW),
        )


    def booking(hours):
        return BookingRequest("user-1", "court-1", NOW, NOW + timedelta(hours=hours))


    def test_within_the_limit_passes():
        assert MaxDurationRule(LIMIT).evaluate(booking(1), context()).passed


    def test_over_the_limit_is_refused():
        assert not MaxDurationRule(LIMIT).evaluate(booking(3), context()).passed


    def test_fails_closed_on_input_it_cannot_evaluate():
        try:
            result = MaxDurationRule(LIMIT).evaluate(object(), context())
        except Exception:
            return
        assert not result.passed
    """)

#: A suite that never finishes. Used only where a real timeout is the thing under test.
HANGING_TESTS = textwrap.dedent("""\
    import time

    from candidate_rule import MaxDurationRule


    def test_never_finishes():
        time.sleep(120)
        assert MaxDurationRule
    """)


class ScriptedClient:
    """An ``LLMClient`` returning the next scripted answer, and recording every prompt it saw.

    The loop calls Generator then Tester against the same client, so the script interleaves them:
    ``[rule_1, tests_1, rule_2, tests_2, ...]``. Running off the end is an error rather than a
    repeat — a loop that made more calls than the test expected is a loop that is not doing what
    the test claims.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, *, system, prompt, model="stub-model"):
        if not self.script:
            raise AssertionError(f"the loop made an unscripted model call: {prompt[:200]!r}")
        self.calls.append({"system": system, "prompt": prompt, "model": model})
        return LLMResponse(text=self.script.pop(0), model=model)

    @property
    def prompts(self):
        return [call["prompt"] for call in self.calls]


def _generator_prompts(client):
    """The prompts sent to Agent A, in order.

    Told apart by system prompt, not by looking for ``<constraint>``: the Tester's prompt quotes the
    constraint too, so a substring match on it silently returns both agents' turns interleaved.
    """
    return [call["prompt"] for call in client.calls if call["system"] is GENERATOR_SYSTEM_PROMPT]


def _tester_prompts(client):
    return [call["prompt"] for call in client.calls if call["system"] is TESTER_SYSTEM_PROMPT]


def sandbox_result(outcome, *, exit_code=1, stdout="", stderr=""):
    return SandboxResult(
        outcome=outcome,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        timeout_seconds=30.0,
    )


# --------------------------------------------------------------------------------------------
# Success
# --------------------------------------------------------------------------------------------


def test_success_on_the_first_attempt_makes_no_retries():
    client = ScriptedClient([GOOD_RULE, TESTS])

    result = run_generation_loop("max 2 hours", client=client, output_dir=None)

    assert result.succeeded
    assert result.attempt_count == 1
    assert result.retries == 0
    assert result.rule_source == GOOD_RULE.strip()
    assert result.test_source == TESTS.strip()
    assert result.last_failure is None
    assert result.attempts[0].outcome is AttemptOutcome.PASSED
    assert len(client.calls) == 2  # one Generator call, one Tester call. No more.


def test_success_after_two_retries_reports_the_accounting():
    client = ScriptedClient([WRONG_RULE, TESTS, WRONG_RULE, TESTS, GOOD_RULE, TESTS])

    result = run_generation_loop("max 2 hours", client=client, output_dir=None)

    assert result.succeeded
    assert result.attempt_count == 3
    assert result.retries == 2
    assert [attempt.outcome for attempt in result.attempts] == [
        AttemptOutcome.TESTS_FAILED,
        AttemptOutcome.TESTS_FAILED,
        AttemptOutcome.PASSED,
    ]
    assert result.rule_source == GOOD_RULE.strip()


def test_tests_are_rewritten_for_every_candidate():
    """A suite imports the rule by name; reusing one would blame the rule for a rename."""
    client = ScriptedClient([WRONG_RULE, TESTS, GOOD_RULE, TESTS])

    run_generation_loop("max 2 hours", client=client, output_dir=None)

    assert len(_tester_prompts(client)) == 2


# --------------------------------------------------------------------------------------------
# Giving up
# --------------------------------------------------------------------------------------------


def test_gives_up_after_exactly_three_retries():
    # Four attempts' worth of answers: the initial attempt plus MAX_RETRIES retries.
    client = ScriptedClient([WRONG_RULE, TESTS] * (MAX_RETRIES + 1))

    result = run_generation_loop("max 2 hours", client=client, output_dir=None)

    assert not result.succeeded
    assert result.retries == MAX_RETRIES == 3
    assert result.attempt_count == 4
    assert result.rule_source is None  # nothing unverified is offered as the answer
    assert result.test_source is None
    assert result.artifact_path is None
    assert client.script == []  # the budget was spent exactly, not overrun
    assert all(a.outcome is AttemptOutcome.TESTS_FAILED for a in result.attempts)


def test_max_retries_is_configurable_and_zero_means_one_attempt():
    client = ScriptedClient([WRONG_RULE, TESTS])

    result = run_generation_loop("max 2 hours", client=client, max_retries=0, output_dir=None)

    assert not result.succeeded
    assert result.attempt_count == 1
    assert result.retries == 0


def test_an_unsafe_candidate_is_a_retryable_rejection():
    client = ScriptedClient([UNSAFE_RULE, GOOD_RULE, TESTS])

    result = run_generation_loop("max 2 hours", client=client, output_dir=None)

    assert result.succeeded
    assert result.attempts[0].outcome is AttemptOutcome.RULE_REJECTED
    # The Tester was never asked to write tests for source that will not be executed.
    assert result.attempts[0].test_source is None


def test_a_backend_failure_is_not_retried():
    """An unreachable model is not fixed by another prompt, and burying it under three identical
    failures would hide the cause."""

    class BrokenClient:
        def __init__(self):
            self.calls = 0

        def complete(self, *, system, prompt, model="stub-model"):
            self.calls += 1
            raise LLMCallError("the CLI is not on PATH", exit_code=127)

    client = BrokenClient()

    with pytest.raises(LLMCallError):
        run_generation_loop("max 2 hours", client=client, output_dir=None)

    assert client.calls == 1


# --------------------------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------------------------


def test_a_timeout_is_not_a_success(monkeypatch):
    """The sandbox is patched here: a real timeout costs wall-clock seconds to produce, and what is
    under test is the loop's reading of the outcome, not the sandbox's ability to time out (which
    ``test_sandbox`` covers against a real infinite loop)."""
    monkeypatch.setattr(
        loop_module,
        "run_candidate",
        lambda *args, **kwargs: sandbox_result(SandboxOutcome.TIMEOUT, exit_code=None),
    )
    client = ScriptedClient([GOOD_RULE, TESTS] * (MAX_RETRIES + 1))

    result = run_generation_loop("max 2 hours", client=client, output_dir=None)

    assert not result.succeeded
    assert result.rule_source is None
    assert all(a.outcome is AttemptOutcome.TIMEOUT for a in result.attempts)


def test_a_real_timeout_is_not_a_success():
    """And once for real, with the actual sandbox, on a suite that genuinely hangs."""
    client = ScriptedClient([GOOD_RULE, HANGING_TESTS])

    result = run_generation_loop(
        "max 2 hours",
        client=client,
        max_retries=0,
        timeout_seconds=2.0,
        output_dir=None,
    )

    assert not result.succeeded
    assert result.attempts[0].outcome is AttemptOutcome.TIMEOUT
    assert result.attempts[0].sandbox.outcome is SandboxOutcome.TIMEOUT


def test_a_crash_is_not_a_success(monkeypatch):
    monkeypatch.setattr(
        loop_module,
        "run_candidate",
        lambda *args, **kwargs: sandbox_result(SandboxOutcome.CRASHED, exit_code=2),
    )
    client = ScriptedClient([GOOD_RULE, TESTS])

    result = run_generation_loop("max 2 hours", client=client, max_retries=0, output_dir=None)

    assert not result.succeeded
    assert result.attempts[0].outcome is AttemptOutcome.CRASHED


def test_no_artifact_is_written_when_the_loop_gives_up(tmp_path):
    client = ScriptedClient([WRONG_RULE, TESTS] * (MAX_RETRIES + 1))

    result = run_generation_loop("max 2 hours", client=client, output_dir=tmp_path)

    assert not result.succeeded
    assert result.artifact_path is None
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------------------------
# Feedback
# --------------------------------------------------------------------------------------------


def test_the_failure_text_is_fed_back_into_the_retry_prompt():
    """The point of the loop. The second Generator call must carry the first failure verbatim."""
    client = ScriptedClient([WRONG_RULE, TESTS, GOOD_RULE, TESTS])

    result = run_generation_loop("max 2 hours", client=client, output_dir=None)

    first_failure = result.attempts[0].failure
    assert "test_over_the_limit_is_refused" in first_failure

    generator_prompts = _generator_prompts(client)
    retry_prompt = generator_prompts[1]
    assert "<failure>" in retry_prompt
    assert "test_over_the_limit_is_refused" in retry_prompt
    # And the source being corrected, so the model is fixing rather than guessing again.
    assert "<previous-attempt>" in retry_prompt
    assert "class MaxDurationRule(BaseRule):" in retry_prompt


def test_the_first_prompt_carries_no_failure():
    client = ScriptedClient([GOOD_RULE, TESTS])

    run_generation_loop("max 2 hours", client=client, output_dir=None)

    first = _generator_prompts(client)[0]
    assert "<failure>" not in first
    assert "<previous-attempt>" not in first


def test_a_validator_rejection_is_fed_back_with_the_construct_it_named():
    client = ScriptedClient([UNSAFE_RULE, GOOD_RULE, TESTS])

    result = run_generation_loop("max 2 hours", client=client, output_dir=None)

    assert "rules.interfaces" in result.attempts[0].failure
    assert "rules.interfaces" in _generator_prompts(client)[1]


def test_a_timeout_is_described_as_looping_rather_than_as_output():
    text = describe_failure(sandbox_result(SandboxOutcome.TIMEOUT, exit_code=None))

    assert "did not finish" in text
    assert "loops" in text


def test_a_long_failure_report_is_truncated():
    text = describe_failure(sandbox_result(SandboxOutcome.FAILED, stdout="E" * 20_000), limit=500)

    assert "truncated at 500 characters" in text
    assert len(text) < 1_000


# --------------------------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------------------------


def test_a_verified_candidate_is_written_for_review(tmp_path):
    client = ScriptedClient([GOOD_RULE, TESTS])

    result = run_generation_loop(
        "bookings can be at most 2 hours", client=client, output_dir=tmp_path
    )

    assert result.succeeded
    directory = result.artifact_path
    assert directory.parent == tmp_path
    assert "bookings-can-be-at-most-2-hours" in directory.name

    rule_file = (directory / "rule.py").read_text()
    assert "class MaxDurationRule(BaseRule):" in rule_file
    assert "bookings can be at most 2 hours" in rule_file
    assert "GENERATED" in rule_file
    assert (directory / "test_rule.py").read_text().count("def test_") == 3


def test_the_artifact_holds_the_rule_as_written_not_the_sandbox_assembly(tmp_path):
    """A reviewer judges what gets ported into the canon. The prelude is a loading detail."""
    directory = write_artifact(
        "max 2 hours", rule_source=GOOD_RULE, test_source=TESTS, output_dir=tmp_path
    )

    rule_file = (directory / "rule.py").read_text()
    assert "from engine import" not in rule_file


def test_two_runs_of_one_description_do_not_overwrite_each_other(tmp_path):
    from datetime import datetime, timezone

    first = write_artifact(
        "max 2 hours",
        rule_source=GOOD_RULE,
        test_source=TESTS,
        output_dir=tmp_path,
        now=datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc),
    )
    second = write_artifact(
        "max 2 hours",
        rule_source=WRONG_RULE,
        test_source=TESTS,
        output_dir=tmp_path,
        now=datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
    )

    assert first != second
    assert len(list(tmp_path.iterdir())) == 2


def test_an_empty_description_is_a_caller_error():
    client = ScriptedClient([])

    with pytest.raises(ValueError):
        run_generation_loop("   ", client=client, output_dir=None)
