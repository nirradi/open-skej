"""Tests for report assembly in ``benchmark.py``.

**No live call, no daemon, no subprocess, no loop run.** Everything here is built from synthetic
``LoopResult``s and ``LLMResponse``s, or from ``RecordingClient`` wrapping a fake ``LLMClient`` —
the same reason ``test_loop.py`` never calls a model.
"""

import json

import pytest

import benchmark
from benchmark import (
    GOLDEN_EXAMPLES,
    BenchmarkReport,
    BenchmarkStatus,
    ExampleReport,
    ModelReport,
    RecordingClient,
    build_arg_parser,
    build_example_report,
    resolve_examples,
    resolve_models,
    run_model,
)
from generation.errors import LLMCallError
from generation.llm import DEFAULT_MODEL, LLMResponse
from generation.loop import Attempt, AttemptOutcome, LoopResult

# --------------------------------------------------------------------------------------------
# build_example_report — the pure assembly function
# --------------------------------------------------------------------------------------------


def _attempt(outcome, **kwargs):
    return Attempt(number=1, outcome=outcome, **kwargs)


def test_a_verified_result_reports_verified_and_succeeded():
    result = LoopResult(
        description="max 1 hour",
        succeeded=True,
        attempts=(_attempt(AttemptOutcome.PASSED),),
        rule_source="class R(BaseRule): ...",
        model="qwen2.5:1.5b",
    )
    responses = [
        LLMResponse(text="x", model="qwen2.5:1.5b", input_tokens=10, output_tokens=20),
        LLMResponse(text="y", model="qwen2.5:1.5b", input_tokens=5, output_tokens=8),
    ]

    report = build_example_report(
        "max 1 hour", model="qwen2.5:1.5b", result=result, responses=responses, wall_ms=42.0
    )

    assert report.status is BenchmarkStatus.VERIFIED
    assert report.succeeded is True
    assert report.attempts == 1
    assert report.retries == 0
    assert report.outcomes == ("passed",)
    assert report.llm_calls == 2
    assert report.input_tokens == 15
    assert report.output_tokens == 28
    assert report.wall_ms == 42.0
    assert report.last_failure is None


def test_a_gave_up_result_carries_the_mixed_outcome_sequence_and_last_failure():
    result = LoopResult(
        description="only on weekends",
        succeeded=False,
        attempts=(
            _attempt(AttemptOutcome.RULE_REJECTED, failure="imported rules.interfaces"),
            _attempt(AttemptOutcome.TESTS_FAILED, failure="test_weekend_is_refused failed"),
            _attempt(AttemptOutcome.TIMEOUT, failure="the tests did not finish"),
        ),
        model="qwen2.5:1.5b",
    )

    report = build_example_report(
        "only on weekends", model="qwen2.5:1.5b", result=result, responses=[], wall_ms=1000.0
    )

    assert report.status is BenchmarkStatus.GAVE_UP
    assert report.succeeded is False
    assert report.attempts == 3
    assert report.retries == 2
    # The point of the task: the per-attempt sequence survives, not just a pass/fail bit.
    assert report.outcomes == ("rule_rejected", "tests_failed", "timeout")
    assert report.last_failure == "the tests did not finish"


def test_no_recorded_responses_yields_zero_calls_and_none_metadata():
    result = LoopResult(
        description="d", succeeded=False, attempts=(_attempt(AttemptOutcome.CRASHED),)
    )

    report = build_example_report("d", model="m", result=result, responses=[], wall_ms=5.0)

    assert report.llm_calls == 0
    assert report.input_tokens is None
    assert report.output_tokens is None
    assert report.cost_usd is None
    assert report.llm_duration_ms is None


# --------------------------------------------------------------------------------------------
# The optional-metadata summing rule
# --------------------------------------------------------------------------------------------


def test_present_values_sum():
    result = LoopResult(
        description="d", succeeded=True, attempts=(_attempt(AttemptOutcome.PASSED),)
    )
    responses = [
        LLMResponse(text="a", model="m", cost_usd=0.01, duration_ms=100),
        LLMResponse(text="b", model="m", cost_usd=0.02, duration_ms=200),
    ]

    report = build_example_report("d", model="m", result=result, responses=responses, wall_ms=0.0)

    assert report.cost_usd == pytest.approx(0.03)
    assert report.llm_duration_ms == 300


def test_all_absent_gives_none_not_zero():
    """OllamaClient reports cost_usd=None deliberately; a benchmark that summed that to 0.0 would
    republish a claim the client refused to make."""
    result = LoopResult(
        description="d", succeeded=True, attempts=(_attempt(AttemptOutcome.PASSED),)
    )
    responses = [
        LLMResponse(text="a", model="m", cost_usd=None),
        LLMResponse(text="b", model="m", cost_usd=None),
    ]

    report = build_example_report("d", model="m", result=result, responses=responses, wall_ms=0.0)

    assert report.cost_usd is None


def test_a_mix_of_present_and_absent_still_sums_the_present_ones():
    result = LoopResult(
        description="d", succeeded=True, attempts=(_attempt(AttemptOutcome.PASSED),)
    )
    responses = [
        LLMResponse(text="a", model="m", input_tokens=10),
        LLMResponse(text="b", model="m", input_tokens=None),
        LLMResponse(text="c", model="m", input_tokens=5),
    ]

    report = build_example_report("d", model="m", result=result, responses=responses, wall_ms=0.0)

    assert report.input_tokens == 15


def test_model_report_totals_use_the_same_optional_summing_rule():
    verified = ExampleReport(
        description="a",
        model="m",
        status=BenchmarkStatus.VERIFIED,
        succeeded=True,
        attempts=1,
        retries=0,
        outcomes=("passed",),
        llm_calls=2,
        input_tokens=10,
        output_tokens=20,
        cost_usd=None,
        llm_duration_ms=100,
        wall_ms=50.0,
        last_failure=None,
    )
    gave_up = ExampleReport(
        description="b",
        model="m",
        status=BenchmarkStatus.GAVE_UP,
        succeeded=False,
        attempts=4,
        retries=3,
        outcomes=("tests_failed",) * 4,
        llm_calls=8,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        llm_duration_ms=None,
        wall_ms=200.0,
        last_failure="gave up",
    )

    model_report = ModelReport(client="ollama", model="m", examples=(verified, gave_up))

    assert model_report.success_count == 1
    assert model_report.total == 2
    assert model_report.llm_calls == 10
    assert model_report.input_tokens == 10  # sums the present one; the absent one contributes 0
    assert model_report.cost_usd is None  # both examples reported none
    assert model_report.wall_ms == 250.0


# --------------------------------------------------------------------------------------------
# RecordingClient
# --------------------------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def complete(self, *, system, prompt, model=DEFAULT_MODEL):
        self.calls.append({"system": system, "prompt": prompt, "model": model})
        return self._responses.pop(0)


def test_recording_client_delegates_arguments_unchanged():
    wrapped = _FakeClient([LLMResponse(text="ok", model="m")])
    client = RecordingClient(wrapped)

    client.complete(system="sys", prompt="write a rule", model="qwen2.5:1.5b")

    assert wrapped.calls == [{"system": "sys", "prompt": "write a rule", "model": "qwen2.5:1.5b"}]


def test_recording_client_returns_the_wrapped_response():
    response = LLMResponse(text="ok", model="m")
    wrapped = _FakeClient([response])
    client = RecordingClient(wrapped)

    assert client.complete(system="s", prompt="p", model="m") is response


def test_recording_client_records_every_response_it_saw():
    responses = [LLMResponse(text="a", model="m"), LLMResponse(text="b", model="m")]
    wrapped = _FakeClient(responses)
    client = RecordingClient(wrapped)

    client.complete(system="s", prompt="p1", model="m")
    client.complete(system="s", prompt="p2", model="m")

    assert client.responses == responses


# --------------------------------------------------------------------------------------------
# run_model — the LLMCallError abort-and-skip path
# --------------------------------------------------------------------------------------------


class _AlwaysBrokenClient:
    """An LLMClient whose every call raises LLMCallError, like an unreachable daemon."""

    def complete(self, *, system, prompt, model=DEFAULT_MODEL):
        raise LLMCallError("Ollama's daemon is not answering", exit_code=None)


def test_a_call_error_on_the_first_example_skips_every_later_one():
    reports = run_model(
        ["max 1 hour", "only on weekends", "max 2 times a week"],
        client=_AlwaysBrokenClient(),
        model="qwen2.5:1.5b",
        retries=0,
    )

    assert [r.status for r in reports] == [
        BenchmarkStatus.CALL_ERROR,
        BenchmarkStatus.SKIPPED,
        BenchmarkStatus.SKIPPED,
    ]
    assert "not answering" in reports[0].last_failure
    assert reports[1].description == "only on weekends"
    assert reports[1].last_failure is not None
    assert all(not r.succeeded for r in reports)


def test_a_call_error_report_carries_no_attempts_or_tokens():
    reports = run_model(["max 1 hour"], client=_AlwaysBrokenClient(), model="m", retries=0)

    report = reports[0]
    assert report.attempts == 0
    assert report.llm_calls == 0
    assert report.input_tokens is None
    assert report.outcomes == ()


# --------------------------------------------------------------------------------------------
# resolve_examples / resolve_models — argument resolution
# --------------------------------------------------------------------------------------------


def test_resolve_examples_defaults_to_all_five_in_order():
    assert resolve_examples(None) == list(GOLDEN_EXAMPLES)
    assert resolve_examples([]) == list(GOLDEN_EXAMPLES)


def test_resolve_examples_filters_case_insensitively():
    assert resolve_examples(["WEEKENDS"]) == ["only on weekends"]


def test_resolve_examples_multiple_filters_are_unioned_without_duplicates():
    # "max" matches two descriptions and is processed first, so both land before "weekends"'s
    # single match; resolve_examples preserves match order, not GOLDEN_EXAMPLES order.
    result = resolve_examples(["max", "weekends"])
    assert result == ["max 1 hour", "max 2 times a week", "only on weekends"]


def test_an_unmatched_filter_names_the_available_descriptions():
    with pytest.raises(ValueError) as excinfo:
        resolve_examples(["no such constraint"])
    message = str(excinfo.value)
    assert "no such constraint" in message
    for description in GOLDEN_EXAMPLES:
        assert description in message


def test_resolve_models_uses_the_explicit_list_when_given():
    assert resolve_models("ollama", ["a", "b"]) == ["a", "b"]


def test_resolve_models_defaults_differ_by_client():
    ollama_default = resolve_models("ollama", None)
    cli_default = resolve_models("claude-cli", None)
    assert ollama_default != cli_default
    assert len(ollama_default) == 1
    assert len(cli_default) == 1


# --------------------------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------------------------


def test_model_flag_is_repeatable():
    args = build_arg_parser().parse_args(["--model", "a", "--model", "b"])
    assert args.models == ["a", "b"]


def test_model_flag_defaults_to_none_when_omitted():
    args = build_arg_parser().parse_args([])
    assert args.models is None


def test_example_flag_is_repeatable():
    args = build_arg_parser().parse_args(["--example", "weekends", "--example", "max 1 hour"])
    assert args.examples == ["weekends", "max 1 hour"]


def test_seed_and_temperature_and_retries_have_the_documented_defaults():
    args = build_arg_parser().parse_args([])
    assert args.seed == 0
    assert args.temperature == 0.0
    assert args.retries == benchmark.MAX_RETRIES


def test_an_unmatched_example_filter_is_a_cli_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        benchmark.main(["--example", "no such constraint"])
    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "no such constraint" in stderr


# --------------------------------------------------------------------------------------------
# main — the run parameters it records, and what it exits with
# --------------------------------------------------------------------------------------------


def _stub_run(monkeypatch, captured):
    """Run main() without building a client or calling anything: capture what it asked for."""

    def fake_build_client(client_name, **kwargs):
        return _AlwaysBrokenClient()

    def fake_run_benchmark(**kwargs):
        captured.update(kwargs)
        return BenchmarkReport(
            client=kwargs["client_name"],
            seed=kwargs["seed"],
            temperature=kwargs["temperature"],
            retries=kwargs["retries"],
            generated_at="2026-07-31T00:00:00+00:00",
            models=(),
        )

    monkeypatch.setattr(benchmark, "build_client", fake_build_client)
    monkeypatch.setattr(benchmark, "run_benchmark", fake_run_benchmark)


def test_a_claude_cli_run_records_no_seed_or_temperature(monkeypatch, capsys):
    """They are only ever handed to the ollama client, so only an ollama run may claim them."""
    captured: dict = {}
    _stub_run(monkeypatch, captured)

    benchmark.main(["--client", "claude-cli", "--example", "max 1 hour"])

    assert captured["seed"] is None
    assert captured["temperature"] is None


def test_an_ollama_run_records_the_seed_and_temperature_it_used(monkeypatch, capsys):
    captured: dict = {}
    _stub_run(monkeypatch, captured)

    benchmark.main(["--seed", "7", "--temperature", "0.2", "--example", "max 1 hour"])

    assert captured["seed"] == 7
    assert captured["temperature"] == pytest.approx(0.2)


def test_a_run_that_only_gave_up_exits_zero(monkeypatch, capsys):
    """A model that cannot hold the contract is the answer this benchmark exists to get."""
    gave_up = ExampleReport(
        description="max 1 hour",
        model="m",
        status=BenchmarkStatus.GAVE_UP,
        succeeded=False,
        attempts=4,
        retries=3,
        outcomes=("tests_failed",) * 4,
        llm_calls=8,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        llm_duration_ms=None,
        wall_ms=1.0,
        last_failure="gave up",
    )
    report = BenchmarkReport(
        client="ollama",
        seed=0,
        temperature=0.0,
        retries=3,
        generated_at="2026-07-31T00:00:00+00:00",
        models=(ModelReport(client="ollama", model="m", examples=(gave_up,)),),
    )

    assert benchmark.any_call_error(report) is False


def test_a_run_that_could_not_reach_the_backend_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(benchmark, "build_client", lambda name, **kwargs: _AlwaysBrokenClient())

    exit_code = benchmark.main(["--example", "max 1 hour", "--retries", "0"])

    assert exit_code == 1


# --------------------------------------------------------------------------------------------
# JSON serialisation
# --------------------------------------------------------------------------------------------


def test_a_full_report_round_trips_through_json_dumps():
    verified = ExampleReport(
        description="max 1 hour",
        model="qwen2.5:1.5b",
        status=BenchmarkStatus.VERIFIED,
        succeeded=True,
        attempts=1,
        retries=0,
        outcomes=("passed",),
        llm_calls=2,
        input_tokens=10,
        output_tokens=20,
        cost_usd=None,
        llm_duration_ms=100,
        wall_ms=50.0,
        last_failure=None,
    )
    call_error = ExampleReport(
        description="only on weekends",
        model="qwen2.5:1.5b",
        status=BenchmarkStatus.CALL_ERROR,
        succeeded=False,
        attempts=0,
        retries=0,
        outcomes=(),
        llm_calls=0,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        llm_duration_ms=None,
        wall_ms=5.0,
        last_failure="Ollama's daemon is not answering",
    )
    model_report = ModelReport(
        client="ollama", model="qwen2.5:1.5b", examples=(verified, call_error)
    )
    report = BenchmarkReport(
        client="ollama",
        seed=0,
        temperature=0.0,
        retries=3,
        generated_at="2026-07-31T00:00:00+00:00",
        models=(model_report,),
    )

    serialised = json.dumps(report.to_dict())
    restored = json.loads(serialised)

    assert restored["client"] == "ollama"
    assert restored["models"][0]["examples"][0]["status"] == "verified"
    assert restored["models"][0]["examples"][1]["status"] == "call_error"
    assert restored["models"][0]["examples"][1]["cost_usd"] is None
    assert restored["models"][0]["examples"][0]["outcomes"] == ["passed"]
