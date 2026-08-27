"""Pure tests for ``shape_benchmark``; no live model, subprocess, or network call."""

from __future__ import annotations

import json
from datetime import date

import pytest

import shape_benchmark
from generation.errors import LLMCallError
from generation.llm import LLMResponse
from shape import ShapeAgentResult, validate_shape
from shape_benchmark import (
    GOLDEN_SHAPE_EXAMPLES,
    GoldenShapeExample,
    OffersExactly,
    OffersNothing,
    OffersProjectionExactly,
    Permits,
    ShapeBenchmarkCheckpoint,
    ShapeBenchmarkReport,
    ShapeBenchmarkStatus,
    ShapeExampleReport,
    ShapeModelReport,
    build_example_report,
    golden_fingerprint,
    run_benchmark,
    run_model,
)


def _document(**overrides):
    document = {
        "version": 1,
        "operating_blocks": [
            {
                "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                "start_time": "09:00",
                "end_time": "17:00",
                "allowed_durations_mins": [60],
            }
        ],
        "blackout_windows": [],
    }
    document.update(overrides)
    return document


def _result(document=None):
    return ShapeAgentResult(
        document=validate_shape(document or _document()),
        summary="Open from 09:00 to 17:00 every day.",
        question=None,
    )


def _example(prompt="A prompt"):
    return GoldenShapeExample(prompt, ())


def _report(prompt="A prompt", *, model="m", status=ShapeBenchmarkStatus.VERIFIED):
    return ShapeExampleReport(
        prompt=prompt,
        model=model,
        status=status,
        succeeded=status is ShapeBenchmarkStatus.VERIFIED,
        attempts=1,
        retries=0,
        llm_calls=1,
        input_tokens=1,
        output_tokens=2,
        cost_usd=None,
        llm_duration_ms=None,
        wall_ms=1.0,
        last_failure=None,
    )


class FakeClient:
    default_model = "fake"

    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def complete(self, *, system, prompt, model=None):
        self.calls.append((system, prompt, model))
        return LLMResponse(text=self.texts.pop(0), model=model or self.default_model)


class BrokenClient:
    default_model = "broken"

    def __init__(self):
        self.calls = 0

    def complete(self, *, system, prompt, model=None):
        self.calls += 1
        raise LLMCallError("unavailable")


def _completion(document=None):
    return json.dumps(
        {
            "document": document or _document(),
            "summary": "Open from 09:00 to 17:00 every day.",
            "question": None,
        }
    )


def test_offers_exactly_checks_the_projection_not_the_document_encoding():
    shape = validate_shape(
        _document(
            operating_blocks=[
                {
                    "days": ["TUE"],
                    "start_time": "18:00",
                    "end_time": "20:00",
                    "allowed_durations_mins": [20],
                }
            ],
            # This differs from the worked example's two-block encoding but offers the same slots.
            blackout_windows=[{"start_time": "19:30", "end_time": "19:40", "reason": "Break"}],
        )
    )
    expectation = OffersExactly(
        date(2026, 8, 18), ("18:00", "18:20", "18:40", "19:00", "19:40"), 20
    )

    assert expectation.evaluate(shape).passed is True


def test_offers_nothing_and_permits_report_projection_failures():
    shape = validate_shape(
        _document(
            operating_blocks=[
                {
                    "days": ["TUE"],
                    "start_time": "09:00",
                    "end_time": "17:00",
                    "allowed_durations_mins": [30],
                }
            ]
        )
    )

    missing = OffersNothing(date(2026, 8, 18), "09:00").evaluate(shape)
    refused = Permits(date(2026, 8, 18), "09:00", 60, False).evaluate(shape)
    wrongly_permitted = Permits(date(2026, 8, 18), "09:00", 60, True).evaluate(shape)

    assert missing.passed is False
    assert "30" in missing.failure
    assert refused.passed is True
    assert wrongly_permitted.passed is False


def test_build_example_report_records_document_expectations_and_metadata():
    example = GoldenShapeExample(
        "Open Tuesday mornings",
        (
            OffersExactly(
                date(2026, 8, 18),
                ("09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00"),
                60,
            ),
        ),
    )
    report = build_example_report(
        example,
        model="m",
        result=_result(),
        responses=[LLMResponse(text="x", model="m", input_tokens=4, output_tokens=5)],
        wall_ms=2.0,
    )

    assert report.status is ShapeBenchmarkStatus.VERIFIED
    assert report.succeeded is True
    assert report.retries == 0
    assert report.document["operating_blocks"][0]["start_time"] == "09:00"
    assert report.expectations[0].passed is True
    assert report.input_tokens == 4


def test_a_valid_shape_that_fails_its_projection_expectation_is_a_result_not_a_call_error():
    example = GoldenShapeExample("Wrong hours", (OffersNothing(date(2026, 8, 18), "09:00"),))
    report = build_example_report(example, model="m", result=_result(), responses=[], wall_ms=1.0)

    assert report.status is ShapeBenchmarkStatus.VERIFIED
    assert report.succeeded is False
    assert report.expectations[0].passed is False


def test_invalid_candidates_are_a_gave_up_result_and_record_the_retry():
    invalid = _completion(
        {"version": 1, "operating_blocks": [{"days": ["MON"]}], "blackout_windows": []}
    )
    reports = run_model([_example()], client=FakeClient(invalid, invalid), model="m")

    assert reports[0].status is ShapeBenchmarkStatus.GAVE_UP
    assert reports[0].llm_calls == 2
    assert reports[0].retries == 1


def test_a_call_error_skips_remaining_examples_for_that_model():
    client = BrokenClient()
    reports = run_model([_example("one"), _example("two")], client=client, model="m")

    assert [report.status for report in reports] == [
        ShapeBenchmarkStatus.CALL_ERROR,
        ShapeBenchmarkStatus.SKIPPED,
    ]
    assert client.calls == 1


def test_checkpoint_round_trips_and_refuses_different_parameters(tmp_path):
    path = tmp_path / "shape.json"
    examples = [_example()]
    checkpoint = ShapeBenchmarkCheckpoint.start(
        path, client_name="google", seed=None, temperature=0.0, examples=examples
    )
    checkpoint.record("m", _report())

    resumed = ShapeBenchmarkCheckpoint.start(
        path, client_name="google", seed=None, temperature=0.0, examples=examples
    )
    assert resumed.completed_for("m")["A prompt"].prompt == "A prompt"

    with pytest.raises(ValueError, match="incompatible"):
        ShapeBenchmarkCheckpoint.start(
            path, client_name="google", seed=None, temperature=0.4, examples=examples
        )


def test_checkpoint_refuses_a_changed_or_reordered_golden_set(tmp_path):
    path = tmp_path / "shape.json"
    original = [_example("one"), _example("two")]
    checkpoint = ShapeBenchmarkCheckpoint.start(
        path, client_name="google", seed=None, temperature=0.0, examples=original
    )
    checkpoint.record("m", _report("one"))

    assert golden_fingerprint(original) != golden_fingerprint(list(reversed(original)))
    with pytest.raises(ValueError, match="golden"):
        ShapeBenchmarkCheckpoint.start(
            path,
            client_name="google",
            seed=None,
            temperature=0.0,
            examples=list(reversed(original)),
        )


def test_run_benchmark_reuses_checkpointed_examples_without_contacting_the_client(tmp_path):
    path = tmp_path / "shape.json"
    examples = [_example("one"), _example("two")]
    checkpoint = ShapeBenchmarkCheckpoint.start(
        path, client_name="google", seed=None, temperature=0.0, examples=examples
    )
    checkpoint.record("m", _report("one"))

    resumed = ShapeBenchmarkCheckpoint.start(
        path, client_name="google", seed=None, temperature=0.0, examples=examples
    )
    client = FakeClient(_completion())
    report = run_benchmark(
        client_name="google",
        client=client,
        models=["m"],
        examples=examples,
        seed=None,
        temperature=0.0,
        checkpoint=resumed,
    )

    assert [item.prompt for item in report.models[0].examples] == ["one", "two"]
    assert len(client.calls) == 1


def test_the_fixed_thin_set_has_exactly_the_three_core_examples_in_order():
    prompts = [example.prompt for example in GOLDEN_SHAPE_EXAMPLES]
    assert len(prompts) == 3
    assert "teacher" in prompts[0]
    assert "lab equipment" in prompts[1]
    assert "music room" in prompts[2]
    assert isinstance(GOLDEN_SHAPE_EXAMPLES[2].expectations[0], OffersProjectionExactly)


def test_each_fixed_example_passes_against_its_hand_written_projection():
    documents = (
        _document(
            operating_blocks=[
                {
                    "days": ["TUE"],
                    "start_time": "18:00",
                    "end_time": "20:00",
                    "allowed_durations_mins": [20],
                }
            ],
            blackout_windows=[{"start_time": "19:30", "end_time": "19:40", "reason": "Break"}],
        ),
        _document(
            operating_blocks=[
                {
                    "days": ["WED"],
                    "start_time": "08:00",
                    "end_time": "17:00",
                    "allowed_durations_mins": [30],
                }
            ],
            blackout_windows=[
                {"start_time": "10:00", "end_time": "10:20", "reason": "Cooldown"},
                {"start_time": "13:00", "end_time": "13:20", "reason": "Cooldown"},
                {"start_time": "15:00", "end_time": "15:20", "reason": "Cooldown"},
            ],
        ),
        _document(
            operating_blocks=[
                {
                    "days": ["THU"],
                    "start_time": "08:00",
                    "end_time": "14:00",
                    "allowed_durations_mins": [60],
                },
                {
                    "days": ["THU"],
                    "start_time": "14:00",
                    "end_time": "22:00",
                    "allowed_durations_mins": [60, 120],
                },
            ]
        ),
    )

    for example, document in zip(GOLDEN_SHAPE_EXAMPLES, documents, strict=True):
        assert all(
            expectation.evaluate(validate_shape(document)).passed
            for expectation in example.expectations
        )


def test_google_records_seed_as_unset_and_temperature_as_applied(monkeypatch):
    captured = {}

    class Client:
        default_model = "m"

    monkeypatch.setattr(shape_benchmark, "build_client", lambda *args, **kwargs: Client())
    monkeypatch.setattr(
        shape_benchmark,
        "run_benchmark",
        lambda **kwargs: captured.update(kwargs)
        or type("R", (), {"models": (), "to_dict": lambda self: {}})(),
    )
    monkeypatch.setattr(shape_benchmark, "print_summary", lambda report: None)

    assert shape_benchmark.main(["--client", "google", "--example", "teacher"]) == 0
    assert captured["seed"] is None
    assert captured["temperature"] == 0.0


def test_cached_call_error_aborts_remaining_examples_without_contacting_the_client():
    reports = run_model(
        [_example("one"), _example("two")],
        client=BrokenClient(),
        model="m",
        completed={"one": _report("one", status=ShapeBenchmarkStatus.CALL_ERROR)},
    )

    assert [report.status for report in reports] == [
        ShapeBenchmarkStatus.CALL_ERROR,
        ShapeBenchmarkStatus.SKIPPED,
    ]


def test_model_totals_and_json_keep_unknown_metadata_as_none():
    examples = (
        ShapeExampleReport(
            prompt="one",
            model="m",
            status=ShapeBenchmarkStatus.VERIFIED,
            succeeded=True,
            attempts=1,
            retries=0,
            llm_calls=1,
            input_tokens=2,
            output_tokens=3,
            cost_usd=0.2,
            llm_duration_ms=10,
            wall_ms=20.0,
            last_failure=None,
        ),
        ShapeExampleReport(
            prompt="two",
            model="m",
            status=ShapeBenchmarkStatus.GAVE_UP,
            succeeded=False,
            attempts=2,
            retries=1,
            llm_calls=2,
            input_tokens=None,
            output_tokens=5,
            cost_usd=None,
            llm_duration_ms=None,
            wall_ms=30.0,
            last_failure="bad shape",
        ),
    )
    model = ShapeModelReport("google", "m", examples)
    report = ShapeBenchmarkReport(
        "google", None, 0.0, "2026-08-01T00:00:00+00:00", "fingerprint", (model,)
    )

    assert model.success_count == 1
    assert model.llm_calls == 3
    assert model.input_tokens == 2
    assert model.output_tokens == 8
    assert model.cost_usd == pytest.approx(0.2)
    assert model.llm_duration_ms == 10
    assert model.wall_ms == 50.0
    encoded = report.to_dict()
    assert encoded["golden_fingerprint"] == "fingerprint"
    assert encoded["models"][0]["output_tokens"] == 8

    unknown = ShapeModelReport("ollama", "m", (_report("unknown"),))
    assert unknown.cost_usd is None
    assert unknown.llm_duration_ms is None


def test_main_returns_nonzero_for_a_call_error(monkeypatch):
    class Client:
        default_model = "m"

    monkeypatch.setattr(shape_benchmark, "build_client", lambda *args, **kwargs: Client())
    monkeypatch.setattr(shape_benchmark, "print_summary", lambda report: None)
    monkeypatch.setattr(
        shape_benchmark,
        "run_benchmark",
        lambda **kwargs: ShapeBenchmarkReport(
            "google",
            None,
            0.0,
            "2026-08-01T00:00:00+00:00",
            "fingerprint",
            (
                ShapeModelReport(
                    "google",
                    "m",
                    (_report("teacher", status=ShapeBenchmarkStatus.CALL_ERROR),),
                ),
            ),
        ),
    )

    assert shape_benchmark.main(["--client", "google", "--example", "teacher"]) == 1


def test_main_reports_unmatched_filter_and_checkpoint_parameter_errors(
    tmp_path, monkeypatch, capsys
):
    with pytest.raises(SystemExit) as unmatched:
        shape_benchmark.main(["--example", "not in the set"])
    assert unmatched.value.code == 2
    assert "no golden shape example matches" in capsys.readouterr().err

    path = tmp_path / "shape.json"
    ShapeBenchmarkCheckpoint.start(
        path,
        client_name="google",
        seed=None,
        temperature=0.0,
        examples=GOLDEN_SHAPE_EXAMPLES,
    ).record("m", _report(GOLDEN_SHAPE_EXAMPLES[0].prompt))
    monkeypatch.setattr(shape_benchmark, "build_client", lambda *args, **kwargs: FakeClient())
    with pytest.raises(SystemExit) as mismatched:
        shape_benchmark.main(
            ["--client", "google", "--temperature", "0.2", "--checkpoint", str(path)]
        )
    assert mismatched.value.code == 2
    assert "incompatible" in capsys.readouterr().err
