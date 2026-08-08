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
    BenchmarkCheckpoint,
    BenchmarkReport,
    BenchmarkStatus,
    ExampleReport,
    ModelReport,
    RecordingClient,
    build_arg_parser,
    build_client,
    build_example_report,
    resolve_examples,
    resolve_models,
    run_benchmark,
    run_model,
)
from generation.errors import LLMCallError
from generation.llm import (
    DEFAULT_GOOGLE_TIMEOUT_SECONDS,
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    ClaudeCliClient,
    GoogleAIStudioClient,
    LLMResponse,
    OllamaClient,
)
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
    default_model = "fake-model"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def complete(self, *, system, prompt, model=None):
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
    default_model = "fake-model"

    """An LLMClient whose every call raises LLMCallError, like an unreachable daemon."""

    def complete(self, *, system, prompt, model=None):
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
    assert resolve_models(OllamaClient(), ["a", "b"]) == ["a", "b"]


def test_resolve_models_defaults_differ_by_client():
    ollama_default = resolve_models(OllamaClient(), None)
    cli_default = resolve_models(ClaudeCliClient(), None)
    assert ollama_default != cli_default
    assert len(ollama_default) == 1
    assert len(cli_default) == 1


def test_resolve_models_google_default_is_the_measured_model():
    # Pinned to the id, not just to "whatever the constant says", because this one is a finding:
    # the model that took all five golden examples on the first attempt. A default that drifts
    # silently is a default nobody measured. Not a floating `-latest` alias either — a benchmark
    # whose model id changes under it cannot compare one run against the next.
    client = GoogleAIStudioClient(api_key="unused")
    assert resolve_models(client, None) == [benchmark.DEFAULT_GOOGLE_MODEL]
    assert benchmark.DEFAULT_GOOGLE_MODEL == "gemini-3.1-flash-lite"


def test_resolve_models_reads_the_client_not_a_table_of_client_names():
    """The regression this whole seam exists for.

    A name-to-model table here was the project's only copy, so a caller that could not import this
    module — the backend's job runner — had no way to ask, and defaulted every client to the CLI's
    Anthropic model id. Asking the client itself is what makes that impossible to reintroduce: a
    new client arrives carrying its own default and nothing here has to learn about it.
    """

    class NewBackendClient:
        default_model = "some-model-nobody-here-knows-about"

        def complete(self, *, system, prompt, model=None):  # pragma: no cover - never called
            raise AssertionError("resolve_models must not call the client")

    assert resolve_models(NewBackendClient(), None) == ["some-model-nobody-here-knows-about"]


# --------------------------------------------------------------------------------------------
# build_client — one client per --client value
# --------------------------------------------------------------------------------------------


def test_build_client_google_returns_a_google_ai_studio_client_with_the_resolved_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_STUDIO_API_KEY", "test-key")

    client = build_client(
        "google", base_url="http://localhost:11434", timeout_seconds=5, seed=0, temperature=0.3
    )

    assert isinstance(client, GoogleAIStudioClient)
    assert client.api_key == "test-key"
    assert client.temperature == pytest.approx(0.3)


def test_build_client_ollama_ignores_the_google_only_key_lookup(monkeypatch):
    monkeypatch.delenv("GOOGLE_STUDIO_API_KEY", raising=False)

    client = build_client(
        "ollama", base_url="http://localhost:11434", timeout_seconds=5, seed=1, temperature=0.2
    )

    assert isinstance(client, OllamaClient)


def test_build_client_google_takes_the_timeout_flag(monkeypatch):
    # --timeout is not an ollama-only knob. A thinking-heavy hosted model can spend the whole of
    # GoogleAIStudioClient's own 120s default on one generation, which the benchmark then records
    # as an unreachable backend — a statement about the wall clock, not about whether the model
    # holds the rule contract. Measured against gemini-3-flash-preview.
    monkeypatch.setenv("GOOGLE_STUDIO_API_KEY", "test-key")

    client = build_client(
        "google", base_url="http://localhost:11434", timeout_seconds=420, seed=0, temperature=0.0
    )

    assert isinstance(client, GoogleAIStudioClient)
    assert client.timeout_seconds == pytest.approx(420)


def test_build_client_leaves_each_clients_own_default_timeout_alone_when_unset(monkeypatch):
    # None means "this run named no timeout", not a number this module picks on every client's
    # behalf — the two clients' defaults differ deliberately and neither is the other's.
    monkeypatch.setenv("GOOGLE_STUDIO_API_KEY", "test-key")

    google = build_client(
        "google", base_url="http://localhost:11434", timeout_seconds=None, seed=0, temperature=0.0
    )
    ollama = build_client(
        "ollama", base_url="http://localhost:11434", timeout_seconds=None, seed=0, temperature=0.0
    )

    assert google.timeout_seconds == pytest.approx(DEFAULT_GOOGLE_TIMEOUT_SECONDS)
    assert ollama.timeout_seconds == pytest.approx(DEFAULT_OLLAMA_TIMEOUT_SECONDS)


# --------------------------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------------------------


def test_timeout_flag_defaults_to_none_so_no_client_default_is_overwritten():
    assert build_arg_parser().parse_args([]).timeout is None


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


def test_a_google_run_records_temperature_but_not_seed(monkeypatch, capsys):
    """AI Studio's generationConfig accepts a seed field but does not honour it (confirmed
    against the live API), so a google run must not claim one — but it does apply temperature,
    unlike claude-cli, so that one is recorded."""
    captured: dict = {}
    _stub_run(monkeypatch, captured)

    benchmark.main(
        ["--client", "google", "--seed", "7", "--temperature", "0.5", "--example", "max 1 hour"]
    )

    assert captured["seed"] is None
    assert captured["temperature"] == pytest.approx(0.5)


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


def test_example_report_round_trips_through_to_dict_and_from_dict():
    original = ExampleReport(
        description="max 1 hour",
        model="qwen2.5:1.5b",
        status=BenchmarkStatus.GAVE_UP,
        succeeded=False,
        attempts=2,
        retries=1,
        outcomes=("rule_rejected", "tests_failed"),
        llm_calls=4,
        input_tokens=100,
        output_tokens=None,
        cost_usd=0.01,
        llm_duration_ms=5000,
        wall_ms=6000.0,
        last_failure="a dunder attribute",
    )

    restored = ExampleReport.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored == original


# --------------------------------------------------------------------------------------------
# BenchmarkCheckpoint — resuming a killed run
# --------------------------------------------------------------------------------------------


def _example(description, model="m", status=BenchmarkStatus.VERIFIED, succeeded=True):
    return ExampleReport(
        description=description,
        model=model,
        status=status,
        succeeded=succeeded,
        attempts=1,
        retries=0,
        outcomes=("passed",) if succeeded else (),
        llm_calls=1,
        input_tokens=1,
        output_tokens=1,
        cost_usd=None,
        llm_duration_ms=1,
        wall_ms=1.0,
        last_failure=None,
    )


def test_a_fresh_checkpoint_has_nothing_completed_for_any_model(tmp_path):
    checkpoint = BenchmarkCheckpoint.start(
        tmp_path / "bench.json", client_name="ollama", seed=0, temperature=0.0, retries=3
    )

    assert checkpoint.completed_for("qwen2.5:1.5b") == {}


def test_record_flushes_to_disk_and_is_visible_through_completed_for(tmp_path):
    path = tmp_path / "bench.json"
    checkpoint = BenchmarkCheckpoint.start(
        path, client_name="ollama", seed=0, temperature=0.0, retries=3
    )

    checkpoint.record("qwen2.5:1.5b", _example("max 1 hour"))

    assert path.exists()
    assert checkpoint.completed_for("qwen2.5:1.5b")["max 1 hour"].description == "max 1 hour"
    on_disk = json.loads(path.read_text())
    assert on_disk["models"][0]["examples"][0]["description"] == "max 1 hour"


def test_starting_again_at_the_same_path_resumes_what_was_recorded(tmp_path):
    path = tmp_path / "bench.json"
    first = BenchmarkCheckpoint.start(
        path, client_name="ollama", seed=0, temperature=0.0, retries=3
    )
    first.record("qwen2.5:1.5b", _example("max 1 hour"))
    first.record("qwen2.5:1.5b", _example("only on weekends", succeeded=False))

    resumed = BenchmarkCheckpoint.start(
        path, client_name="ollama", seed=0, temperature=0.0, retries=3
    )

    completed = resumed.completed_for("qwen2.5:1.5b")
    assert set(completed) == {"max 1 hour", "only on weekends"}
    assert completed["max 1 hour"].succeeded is True
    assert completed["only on weekends"].succeeded is False


def test_a_model_never_recorded_resumes_with_nothing_completed(tmp_path):
    path = tmp_path / "bench.json"
    first = BenchmarkCheckpoint.start(
        path, client_name="ollama", seed=0, temperature=0.0, retries=3
    )
    first.record("qwen2.5:1.5b", _example("max 1 hour"))

    resumed = BenchmarkCheckpoint.start(
        path, client_name="ollama", seed=0, temperature=0.0, retries=3
    )

    assert resumed.completed_for("llama3.1:8b") == {}


def test_resuming_with_a_different_seed_raises_rather_than_mixing_results(tmp_path):
    path = tmp_path / "bench.json"
    BenchmarkCheckpoint.start(
        path, client_name="ollama", seed=0, temperature=0.0, retries=3
    ).record("m", _example("max 1 hour"))

    with pytest.raises(ValueError) as excinfo:
        BenchmarkCheckpoint.start(path, client_name="ollama", seed=7, temperature=0.0, retries=3)

    message = str(excinfo.value)
    assert "seed=0" in message
    assert "seed=7" in message


def test_resuming_with_a_different_client_raises(tmp_path):
    path = tmp_path / "bench.json"
    BenchmarkCheckpoint.start(
        path, client_name="ollama", seed=0, temperature=0.0, retries=3
    ).record("m", _example("max 1 hour"))

    with pytest.raises(ValueError):
        BenchmarkCheckpoint.start(
            path, client_name="claude-cli", seed=None, temperature=None, retries=3
        )


# --------------------------------------------------------------------------------------------
# run_model — reusing checkpointed examples instead of re-running them
# --------------------------------------------------------------------------------------------


def test_run_model_returns_a_cached_example_without_touching_the_client():
    cached = _example("max 1 hour")

    reports = run_model(
        ["max 1 hour"],
        client=_AlwaysBrokenClient(),
        model="m",
        retries=0,
        completed={"max 1 hour": cached},
    )

    assert reports == [cached]


def test_run_model_calls_on_example_only_for_freshly_computed_reports(monkeypatch):
    fresh_result = LoopResult(
        description="only on weekends",
        succeeded=True,
        attempts=(_attempt(AttemptOutcome.PASSED),),
        rule_source="class R(BaseRule): ...",
        model="m",
    )
    monkeypatch.setattr(benchmark, "run_generation_loop", lambda *a, **kw: fresh_result)
    recorded = []

    reports = run_model(
        ["max 1 hour", "only on weekends"],
        client=_AlwaysBrokenClient(),
        model="m",
        retries=0,
        completed={"max 1 hour": _example("max 1 hour")},
        on_example=recorded.append,
    )

    assert [r.description for r in reports] == ["max 1 hour", "only on weekends"]
    # Only the fresh one was handed to on_example; the cached one is already persisted.
    assert [r.description for r in recorded] == ["only on weekends"]


def test_run_model_resuming_past_a_recorded_call_error_skips_without_recontacting_the_client(
    monkeypatch,
):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("run_generation_loop must not be called for an aborted model")

    monkeypatch.setattr(benchmark, "run_generation_loop", fail_if_called)

    reports = run_model(
        ["max 1 hour", "only on weekends"],
        client=_AlwaysBrokenClient(),
        model="m",
        retries=0,
        completed={
            "max 1 hour": _example("max 1 hour", status=BenchmarkStatus.CALL_ERROR, succeeded=False)
        },
    )

    assert reports[0].status is BenchmarkStatus.CALL_ERROR
    assert reports[1].status is BenchmarkStatus.SKIPPED


# --------------------------------------------------------------------------------------------
# run_benchmark + BenchmarkCheckpoint — the end-to-end resume path
# --------------------------------------------------------------------------------------------


def test_run_benchmark_with_a_checkpoint_skips_examples_already_recorded(tmp_path, monkeypatch):
    path = tmp_path / "bench.json"
    seeded = BenchmarkCheckpoint.start(
        path, client_name="ollama", seed=0, temperature=0.0, retries=0
    )
    seeded.record("m", _example("max 1 hour"))

    calls = []

    def fake_loop(description, **kwargs):
        calls.append(description)
        return LoopResult(
            description=description,
            succeeded=True,
            attempts=(_attempt(AttemptOutcome.PASSED),),
            rule_source="class R(BaseRule): ...",
            model="m",
        )

    monkeypatch.setattr(benchmark, "run_generation_loop", fake_loop)

    checkpoint = BenchmarkCheckpoint.start(
        path, client_name="ollama", seed=0, temperature=0.0, retries=0
    )
    report = run_benchmark(
        client_name="ollama",
        client=_AlwaysBrokenClient(),
        models=["m"],
        descriptions=["max 1 hour", "only on weekends"],
        retries=0,
        seed=0,
        temperature=0.0,
        checkpoint=checkpoint,
    )

    # "max 1 hour" was already in the checkpoint; only the new description hit the loop.
    assert calls == ["only on weekends"]
    assert [e.description for e in report.models[0].examples] == ["max 1 hour", "only on weekends"]
    on_disk = json.loads(path.read_text())
    assert {e["description"] for e in on_disk["models"][0]["examples"]} == {
        "max 1 hour",
        "only on weekends",
    }


# --------------------------------------------------------------------------------------------
# main — wiring --checkpoint through
# --------------------------------------------------------------------------------------------


def test_main_with_checkpoint_passes_a_checkpoint_matching_the_run_parameters(
    tmp_path, monkeypatch
):
    captured: dict = {}
    _stub_run(monkeypatch, captured)

    benchmark.main(
        [
            "--example",
            "max 1 hour",
            "--checkpoint",
            str(tmp_path / "bench.json"),
        ]
    )

    checkpoint = captured["checkpoint"]
    assert isinstance(checkpoint, BenchmarkCheckpoint)
    assert checkpoint.client_name == "ollama"
    assert checkpoint.seed == 0
    assert checkpoint.temperature == pytest.approx(0.0)


def test_main_refuses_to_resume_a_checkpoint_with_different_parameters(tmp_path, capsys):
    path = tmp_path / "bench.json"
    BenchmarkCheckpoint.start(
        path, client_name="ollama", seed=0, temperature=0.0, retries=benchmark.MAX_RETRIES
    ).record("m", _example("max 1 hour"))

    with pytest.raises(SystemExit) as excinfo:
        benchmark.main(["--example", "max 1 hour", "--seed", "9", "--checkpoint", str(path)])

    assert excinfo.value.code == 2
    stderr = capsys.readouterr().err
    assert "seed=0" in stderr
    assert "seed=9" in stderr


def test_main_without_checkpoint_flag_passes_none(monkeypatch):
    captured: dict = {}
    _stub_run(monkeypatch, captured)

    benchmark.main(["--example", "max 1 hour"])

    assert captured["checkpoint"] is None
