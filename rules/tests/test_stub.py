"""Tests for ``StubLLMClient`` — the no-network client CI and the backend's own tests drive.

Every test here runs the *real* generation loop, against the *real* sandbox: the entire point of
this client is that it needs no mocking above the ``LLMClient`` seam, and a test that mocked the
loop on top of it would prove nothing about whether the canned pair actually survives a subprocess
pytest run.
"""

from generation.errors import LLMCallError
from generation.generator import SYSTEM_PROMPT as GENERATOR_SYSTEM_PROMPT
from generation.loop import AttemptOutcome, run_generation_loop
from generation.manifest import SYSTEM_PROMPT as MANIFEST_SYSTEM_PROMPT
from generation.manifest import generate_manifest
from generation.stub import StubLLMClient
from generation.tester import SYSTEM_PROMPT as TESTER_SYSTEM_PROMPT

import pytest


def test_stub_rule_and_tests_pass_in_the_real_sandbox_on_the_first_attempt():
    client = StubLLMClient()

    result = run_generation_loop("max 2 hours", client=client, output_dir=None)

    assert result.succeeded
    assert result.attempt_count == 1
    assert result.attempts[0].outcome is AttemptOutcome.PASSED
    assert result.rule_source is not None
    assert result.test_source is not None


def test_stub_dispatches_on_the_real_system_prompts_not_a_guess():
    client = StubLLMClient()

    generator_answer = client.complete(system=GENERATOR_SYSTEM_PROMPT, prompt="anything")
    tester_answer = client.complete(system=TESTER_SYSTEM_PROMPT, prompt="anything")
    manifest_answer = client.complete(system=MANIFEST_SYSTEM_PROMPT, prompt="anything")

    assert "class StubMaxDurationRule" in generator_answer.text
    assert "candidate_rule" in tester_answer.text
    assert "max_duration" in manifest_answer.text


def test_the_stub_manifest_describes_the_stub_rule_and_survives_the_cross_check():
    """The manifest call made after the loop passes, driven end to end against the stub's own
    canned rule — the same shape the backend's job runner exercises against a real one."""
    client = StubLLMClient()
    result = run_generation_loop("max 2 hours", client=client, output_dir=None)

    manifest = generate_manifest(result.rule_source, "max 2 hours", client=client)

    assert manifest.label
    assert manifest.description
    assert [param.name for param in manifest.params] == ["max_duration"]
    assert manifest.reads_history is False


def test_stub_reports_no_token_usage_rather_than_fabricated_zeros():
    client = StubLLMClient()

    response = client.complete(system=GENERATOR_SYSTEM_PROMPT, prompt="anything")

    assert response.input_tokens is None
    assert response.output_tokens is None
    assert response.cost_usd is None
    assert response.duration_ms is None


def test_stub_rejects_a_system_prompt_neither_agent_sends():
    client = StubLLMClient()

    with pytest.raises(LLMCallError):
        client.complete(system="not a real agent prompt", prompt="anything")


def test_always_unsafe_mode_exhausts_retries_and_never_passes():
    client = StubLLMClient(always_unsafe=True)

    result = run_generation_loop("max 2 hours", client=client, max_retries=3, output_dir=None)

    assert not result.succeeded
    assert result.attempt_count == 4  # the first attempt plus all three retries
    assert all(attempt.outcome is AttemptOutcome.RULE_REJECTED for attempt in result.attempts)
    # Every attempt is a Generator-only call: an attempt whose rule was rejected never reaches
    # the Tester, so this mode never answers a Tester turn at all.
    assert client._generator_calls == 4


def test_fail_first_attempts_makes_a_retried_job_reachable_on_demand():
    """The shape the backend's runner tests need: attempt 1 fails, attempt 2 passes.

    This is the "scripted stub" the stream lead's addendum asks for, in preference to making the
    loop itself non-deterministic: a fresh ``StubLLMClient(fail_first_attempts=1)`` fails its first
    Generator turn (an unsafe candidate, rejected by the validator) and succeeds on its second,
    carrying the previous source and the failure text forward in the second call's *prompt* — which
    is exactly the exchange the backend's job runner must persist and a later test must assert the
    contents of.
    """
    client = StubLLMClient(fail_first_attempts=1)

    result = run_generation_loop("max 2 hours", client=client, output_dir=None)

    assert result.succeeded
    assert result.attempt_count == 2
    assert result.attempts[0].outcome is AttemptOutcome.RULE_REJECTED
    assert result.attempts[1].outcome is AttemptOutcome.PASSED
