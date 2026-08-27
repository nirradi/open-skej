"""Measure the calendar-shape agent against fixed projection expectations.

Invoked by hand, never by pytest.  Unlike a document comparison, every assertion here asks the
same projection the grid and booking gate use what a produced shape offers.  Equivalent encodings
of a break therefore receive the same result, and no LLM judge or extra model call is involved.

    python shape_benchmark.py --client google --model gemini-3.1-flash-lite \\
        --checkpoint shape-benchmark.json --output shape-benchmark.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from generation.errors import LLMCallError
from generation.llm import (
    DEFAULT_GOOGLE_TIMEOUT_SECONDS,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    ClaudeCliClient,
    GoogleAIStudioClient,
    LLMClient,
    LLMResponse,
    OllamaClient,
    RecordingClient,
    read_google_api_key,
)
from shape import (
    DAY_NAMES,
    InvalidShapeError,
    Shape,
    ShapeAgentResponseError,
    generate_shape,
    permits,
    project_day,
)

__all__ = [
    "GOLDEN_SHAPE_EXAMPLES",
    "GoldenShapeExample",
    "OffersExactly",
    "OffersProjectionExactly",
    "OffersNothing",
    "Permits",
    "ExpectationResult",
    "ShapeBenchmarkStatus",
    "ShapeExampleReport",
    "ShapeModelReport",
    "ShapeBenchmarkReport",
    "ShapeBenchmarkCheckpoint",
    "build_example_report",
    "run_model",
    "run_benchmark",
    "resolve_examples",
    "golden_fingerprint",
    "resolve_models",
    "build_client",
    "build_arg_parser",
    "print_summary",
    "any_call_error",
    "main",
]


def _minutes(value: str) -> int:
    """Parse the benchmark's deliberately-small ``HH:MM`` expectation notation."""
    hours, minutes = value.split(":", 1)
    return int(hours) * 60 + int(minutes)


class ProjectionExpectation(Protocol):
    """One pure claim about the projection of a generated shape."""

    def evaluate(self, shape: Shape) -> "ExpectationResult": ...


@dataclass(frozen=True)
class ExpectationResult:
    """A durable, human-readable result of one projection assertion."""

    description: str
    passed: bool
    failure: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"description": self.description, "passed": self.passed, "failure": self.failure}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExpectationResult":
        return cls(data["description"], data["passed"], data.get("failure"))


@dataclass(frozen=True)
class OffersExactly:
    """The date offers exactly these starts, each with exactly one declared duration."""

    on_date: date
    starts: tuple[str, ...]
    duration_mins: int

    def evaluate(self, shape: Shape) -> ExpectationResult:
        expected = tuple(_minutes(start) for start in self.starts)
        actual = tuple(
            offered.start_minutes
            for offered in project_day(shape, self.on_date).offered_starts
            if offered.durations_mins == (self.duration_mins,)
        )
        description = (
            f"{self.on_date.isoformat()} offers exactly {list(self.starts)!r} at "
            f"{self.duration_mins} minutes"
        )
        if actual == expected and len(project_day(shape, self.on_date).offered_starts) == len(
            actual
        ):
            return ExpectationResult(description, True)
        return ExpectationResult(
            description,
            False,
            f"expected starts {list(self.starts)!r} with only {self.duration_mins}-minute offers; "
            f"got {_offered_starts_text(shape, self.on_date)}",
        )


@dataclass(frozen=True)
class OffersNothing:
    """No duration is offered at this local wall-clock start."""

    on_date: date
    at: str

    def evaluate(self, shape: Shape) -> ExpectationResult:
        minute = _minutes(self.at)
        offered = next(
            (
                item
                for item in project_day(shape, self.on_date).offered_starts
                if item.start_minutes == minute
            ),
            None,
        )
        description = f"{self.on_date.isoformat()} offers nothing at {self.at}"
        if offered is None:
            return ExpectationResult(description, True)
        return ExpectationResult(
            description,
            False,
            f"offered {list(offered.durations_mins)!r}-minute durations at {self.at}",
        )


@dataclass(frozen=True)
class OffersProjectionExactly:
    """The complete start-to-duration table projected for one local date.

    This is the strongest shape assertion: it catches an opening or closing hour, an added duration,
    or a missing grid tick without preferring any JSON encoding that produced the table.
    """

    on_date: date
    offers: tuple[tuple[str, tuple[int, ...]], ...]

    def evaluate(self, shape: Shape) -> ExpectationResult:
        projection = project_day(shape, self.on_date)
        expected = tuple((_minutes(start), durations) for start, durations in self.offers)
        actual = tuple(
            (offered.start_minutes, offered.durations_mins) for offered in projection.offered_starts
        )
        description = f"{self.on_date.isoformat()} offers exactly the declared start/duration table"
        if actual == expected:
            return ExpectationResult(description, True)
        return ExpectationResult(
            description,
            False,
            f"expected {list(self.offers)!r}; got {_offered_starts_text(shape, self.on_date)}",
        )


@dataclass(frozen=True)
class Permits:
    """The gate lookup permits (or refuses) one requested start and duration."""

    on_date: date
    start: str
    duration_mins: int
    allowed: bool

    def evaluate(self, shape: Shape) -> ExpectationResult:
        start_minutes = _minutes(self.start)
        verdict = permits(shape, self.on_date, start_minutes, start_minutes + self.duration_mins)
        description = (
            f"{self.on_date.isoformat()} {'permits' if self.allowed else 'refuses'} "
            f"{self.start} for {self.duration_mins} minutes"
        )
        if verdict.allowed is self.allowed:
            return ExpectationResult(description, True)
        return ExpectationResult(
            description,
            False,
            f"projection {'permitted' if verdict.allowed else 'refused'} it"
            + (f": {verdict.reason}" if verdict.reason else ""),
        )


def _offered_starts_text(shape: Shape, on_date: date) -> list[dict[str, Any]]:
    return [
        {
            "start": f"{item.start_minutes // 60:02d}:{item.start_minutes % 60:02d}",
            "durations_mins": list(item.durations_mins),
        }
        for item in project_day(shape, on_date).offered_starts
    ]


@dataclass(frozen=True)
class GoldenShapeExample:
    """One fixed single-turn prompt and the calendar it must offer."""

    prompt: str
    expectations: tuple[ProjectionExpectation, ...]


# The order is fixed.  These are the three core vocabulary examples from the shape-agent prompt;
# Date interpretation and multi-turn cases remain in
# deferred/shape-benchmark-tough-cases.md.
_TUESDAY = date(2026, 8, 18)
_WEDNESDAY = date(2026, 8, 19)
_THURSDAY = date(2026, 8, 20)
GOLDEN_SHAPE_EXAMPLES: tuple[GoldenShapeExample, ...] = (
    GoldenShapeExample(
        "I'm a teacher and parents need to schedule slots with me. 20 minute slots from 18 to 20; "
        "I take a break for 10 min at 1930.",
        (
            OffersExactly(_TUESDAY, ("18:00", "18:20", "18:40", "19:00", "19:40"), 20),
            OffersNothing(_TUESDAY, "19:20"),
        ),
    ),
    GoldenShapeExample(
        "The lab equipment runs from 8 to 5pm, with 3 20 min cooldowns at 10, 13 and 15. "
        "Students can take 30 min slots.",
        (
            OffersExactly(
                _WEDNESDAY,
                (
                    "08:00",
                    "08:30",
                    "09:00",
                    "09:30",
                    "10:30",
                    "11:00",
                    "11:30",
                    "12:00",
                    "12:30",
                    "13:30",
                    "14:00",
                    "14:30",
                    "15:30",
                    "16:00",
                    "16:30",
                ),
                30,
            ),
        ),
    ),
    GoldenShapeExample(
        "The music room is bookable for 1 hour sessions in the morning until 1400 and one or two "
        "hour sessions in the evening until 2200.",
        (
            OffersProjectionExactly(
                _THURSDAY,
                (
                    ("08:00", (60,)),
                    ("09:00", (60,)),
                    ("10:00", (60,)),
                    ("11:00", (60,)),
                    ("12:00", (60,)),
                    ("13:00", (60,)),
                    ("14:00", (60, 120)),
                    ("15:00", (60, 120)),
                    ("16:00", (60, 120)),
                    ("17:00", (60, 120)),
                    ("18:00", (60, 120)),
                    ("19:00", (60, 120)),
                    ("20:00", (60, 120)),
                    ("21:00", (60,)),
                ),
            ),
        ),
    ),
)


class ShapeBenchmarkStatus(str, Enum):
    """How one shape-agent conversation ended, matching ``benchmark.BenchmarkStatus``'s intent."""

    VERIFIED = "verified"
    GAVE_UP = "gave_up"
    CALL_ERROR = "call_error"
    SKIPPED = "skipped"


def _fingerprint_value(value: Any) -> Any:
    """Turn a fixed expectation into stable JSON without relying on its repr()."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_fingerprint_value(item) for item in value]
    if is_dataclass(value):
        return {
            "type": type(value).__qualname__,
            "fields": {
                item.name: _fingerprint_value(getattr(value, item.name)) for item in fields(value)
            },
        }
    return value


def golden_fingerprint(examples: Sequence[GoldenShapeExample]) -> str:
    """Identity of the ordered prompts and expectations a checkpoint was measured against."""
    payload = [
        {"prompt": example.prompt, "expectations": _fingerprint_value(example.expectations)}
        for example in examples
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _shape_to_document(shape: Shape) -> dict[str, Any]:
    def clock(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def date_text(value: date | None) -> str | None:
        return value.isoformat() if value is not None else None

    blocks = []
    for block in shape.operating_blocks:
        item: dict[str, Any] = {
            "days": [DAY_NAMES[day] for day in sorted(block.days)],
            "start_time": clock(block.start_time),
            "end_time": clock(block.end_time),
            "allowed_durations_mins": list(block.allowed_durations_mins),
        }
        if block.effective_from is not None:
            item["effective_from"] = date_text(block.effective_from)
        if block.effective_to is not None:
            item["effective_to"] = date_text(block.effective_to)
        blocks.append(item)
    blackouts = []
    for blackout in shape.blackout_windows:
        item = {
            "start_time": clock(blackout.start_time),
            "end_time": clock(blackout.end_time),
            "reason": blackout.reason,
        }
        if blackout.days is not None:
            item["days"] = [DAY_NAMES[day] for day in sorted(blackout.days)]
        if blackout.date is not None:
            item["date"] = date_text(blackout.date)
        if blackout.effective_from is not None:
            item["effective_from"] = date_text(blackout.effective_from)
        if blackout.effective_to is not None:
            item["effective_to"] = date_text(blackout.effective_to)
        blackouts.append(item)
    return {"version": shape.version, "operating_blocks": blocks, "blackout_windows": blackouts}


@dataclass(frozen=True)
class ShapeExampleReport:
    """One prompt's valid shape, projection assertion results, and observed model cost."""

    prompt: str
    model: str
    status: ShapeBenchmarkStatus
    succeeded: bool
    attempts: int
    retries: int
    llm_calls: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    llm_duration_ms: int | None
    wall_ms: float
    last_failure: str | None
    document: dict[str, Any] | None = None
    summary: str | None = None
    question: str | None = None
    expectations: tuple[ExpectationResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "model": self.model,
            "status": self.status.value,
            "succeeded": self.succeeded,
            "attempts": self.attempts,
            "retries": self.retries,
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "llm_duration_ms": self.llm_duration_ms,
            "wall_ms": self.wall_ms,
            "last_failure": self.last_failure,
            "document": self.document,
            "summary": self.summary,
            "question": self.question,
            "expectations": [item.to_dict() for item in self.expectations],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ShapeExampleReport":
        return cls(
            prompt=data["prompt"],
            model=data["model"],
            status=ShapeBenchmarkStatus(data["status"]),
            succeeded=data["succeeded"],
            attempts=data["attempts"],
            retries=data["retries"],
            llm_calls=data["llm_calls"],
            input_tokens=data["input_tokens"],
            output_tokens=data["output_tokens"],
            cost_usd=data["cost_usd"],
            llm_duration_ms=data["llm_duration_ms"],
            wall_ms=data["wall_ms"],
            last_failure=data["last_failure"],
            document=data.get("document"),
            summary=data.get("summary"),
            question=data.get("question"),
            expectations=tuple(
                ExpectationResult.from_dict(item) for item in data.get("expectations", ())
            ),
        )


def _sum_optional(values: Iterable[int | float | None]) -> int | float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def build_example_report(
    example: GoldenShapeExample,
    *,
    model: str,
    result: Any,
    responses: Sequence[LLMResponse],
    wall_ms: float,
) -> ShapeExampleReport:
    """Pure report assembly for a successfully parsed shape-agent result."""
    expectations = tuple(
        expectation.evaluate(result.document) for expectation in example.expectations
    )
    return ShapeExampleReport(
        prompt=example.prompt,
        model=model,
        status=ShapeBenchmarkStatus.VERIFIED,
        succeeded=all(item.passed for item in expectations),
        attempts=len(responses),
        retries=max(0, len(responses) - 1),
        llm_calls=len(responses),
        input_tokens=_sum_optional(response.input_tokens for response in responses),
        output_tokens=_sum_optional(response.output_tokens for response in responses),
        cost_usd=_sum_optional(response.cost_usd for response in responses),
        llm_duration_ms=_sum_optional(response.duration_ms for response in responses),
        wall_ms=wall_ms,
        last_failure=None,
        document=_shape_to_document(result.document),
        summary=result.summary,
        question=result.question,
        expectations=expectations,
    )


def _failed_report(
    example: GoldenShapeExample,
    *,
    model: str,
    status: ShapeBenchmarkStatus,
    detail: str,
    wall_ms: float,
    responses: Sequence[LLMResponse] = (),
) -> ShapeExampleReport:
    return ShapeExampleReport(
        prompt=example.prompt,
        model=model,
        status=status,
        succeeded=False,
        attempts=len(responses),
        retries=max(0, len(responses) - 1),
        llm_calls=len(responses),
        input_tokens=_sum_optional(response.input_tokens for response in responses),
        output_tokens=_sum_optional(response.output_tokens for response in responses),
        cost_usd=_sum_optional(response.cost_usd for response in responses),
        llm_duration_ms=_sum_optional(response.duration_ms for response in responses),
        wall_ms=wall_ms,
        last_failure=detail,
    )


def _skipped_report(example: GoldenShapeExample, *, model: str) -> ShapeExampleReport:
    return _failed_report(
        example,
        model=model,
        status=ShapeBenchmarkStatus.SKIPPED,
        detail="skipped: an earlier example hit an LLMCallError and aborted this model's run",
        wall_ms=0.0,
    )


@dataclass(frozen=True)
class ShapeModelReport:
    client: str
    model: str
    examples: tuple[ShapeExampleReport, ...]

    @property
    def total(self) -> int:
        return len(self.examples)

    @property
    def success_count(self) -> int:
        return sum(example.succeeded for example in self.examples)

    @property
    def llm_calls(self) -> int:
        return sum(example.llm_calls for example in self.examples)

    @property
    def input_tokens(self) -> int | None:
        return _sum_optional(example.input_tokens for example in self.examples)

    @property
    def output_tokens(self) -> int | None:
        return _sum_optional(example.output_tokens for example in self.examples)

    @property
    def cost_usd(self) -> float | None:
        return _sum_optional(example.cost_usd for example in self.examples)

    @property
    def llm_duration_ms(self) -> int | None:
        return _sum_optional(example.llm_duration_ms for example in self.examples)

    @property
    def wall_ms(self) -> float:
        return sum(example.wall_ms for example in self.examples)

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "model": self.model,
            "success_count": self.success_count,
            "total": self.total,
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "llm_duration_ms": self.llm_duration_ms,
            "wall_ms": self.wall_ms,
            "examples": [example.to_dict() for example in self.examples],
        }


@dataclass(frozen=True)
class ShapeBenchmarkReport:
    client: str
    seed: int | None
    temperature: float | None
    generated_at: str
    golden_fingerprint: str
    models: tuple[ShapeModelReport, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "seed": self.seed,
            "temperature": self.temperature,
            "generated_at": self.generated_at,
            "golden_fingerprint": self.golden_fingerprint,
            "models": [model.to_dict() for model in self.models],
        }


@dataclass
class ShapeBenchmarkCheckpoint:
    """Checkpoint one report per prompt, refusing incomparable parameters or golden sets."""

    path: Path
    client_name: str
    seed: int | None
    temperature: float | None
    golden_fingerprint: str
    generated_at: str
    _completed: dict[str, dict[str, ShapeExampleReport]] = field(default_factory=dict)

    @classmethod
    def start(
        cls,
        path: Path,
        *,
        client_name: str,
        seed: int | None,
        temperature: float | None,
        examples: Sequence[GoldenShapeExample],
    ) -> "ShapeBenchmarkCheckpoint":
        if not path.exists():
            return cls(
                path,
                client_name,
                seed,
                temperature,
                golden_fingerprint(examples),
                datetime.now(timezone.utc).isoformat(),
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        found, wanted = (
            data["client"],
            data["seed"],
            data["temperature"],
            data.get("golden_fingerprint"),
        ), (
            client_name,
            seed,
            temperature,
            golden_fingerprint(examples),
        )
        if found != wanted:
            raise ValueError(
                f"checkpoint at {path} has incompatible client, sampling parameters, or golden "
                f"fingerprint; expected client={client_name!r} seed={seed!r} "
                f"temperature={temperature!r} fingerprint={golden_fingerprint(examples)!r}."
            )
        return cls(
            path,
            client_name,
            seed,
            temperature,
            data["golden_fingerprint"],
            data["generated_at"],
            {
                item["model"]: {
                    example["prompt"]: ShapeExampleReport.from_dict(example)
                    for example in item["examples"]
                }
                for item in data["models"]
            },
        )

    def completed_for(self, model: str) -> dict[str, ShapeExampleReport]:
        return self._completed.get(model, {})

    def record(self, model: str, example: ShapeExampleReport) -> None:
        self._completed.setdefault(model, {})[example.prompt] = example
        self.path.write_text(json.dumps(self._report().to_dict(), indent=2), encoding="utf-8")

    def _report(self) -> ShapeBenchmarkReport:
        return ShapeBenchmarkReport(
            self.client_name,
            self.seed,
            self.temperature,
            self.generated_at,
            self.golden_fingerprint,
            tuple(
                ShapeModelReport(self.client_name, model, tuple(examples.values()))
                for model, examples in self._completed.items()
            ),
        )


def run_model(
    examples: Sequence[GoldenShapeExample],
    *,
    client: LLMClient,
    model: str,
    completed: Mapping[str, ShapeExampleReport] | None = None,
    on_example: Callable[[ShapeExampleReport], None] | None = None,
) -> list[ShapeExampleReport]:
    reports: list[ShapeExampleReport] = []
    aborted = False
    completed = completed or {}
    for example in examples:
        cached = completed.get(example.prompt)
        if cached is not None:
            reports.append(cached)
            aborted = aborted or cached.status is ShapeBenchmarkStatus.CALL_ERROR
            continue
        if aborted:
            report = _skipped_report(example, model=model)
        else:
            recording = RecordingClient(client)
            started = time.perf_counter()
            try:
                result = generate_shape(example.prompt, client=recording, model=model)
            except LLMCallError as exc:
                report = _failed_report(
                    example,
                    model=model,
                    status=ShapeBenchmarkStatus.CALL_ERROR,
                    detail=str(exc),
                    wall_ms=(time.perf_counter() - started) * 1000,
                    responses=recording.responses,
                )
                aborted = True
            except (InvalidShapeError, ShapeAgentResponseError) as exc:
                report = _failed_report(
                    example,
                    model=model,
                    status=ShapeBenchmarkStatus.GAVE_UP,
                    detail=str(exc),
                    wall_ms=(time.perf_counter() - started) * 1000,
                    responses=recording.responses,
                )
            else:
                report = build_example_report(
                    example,
                    model=model,
                    result=result,
                    responses=recording.responses,
                    wall_ms=(time.perf_counter() - started) * 1000,
                )
        reports.append(report)
        if on_example is not None:
            on_example(report)
    return reports


def run_benchmark(
    *,
    client_name: str,
    client: LLMClient,
    models: Sequence[str],
    examples: Sequence[GoldenShapeExample],
    seed: int | None,
    temperature: float | None,
    checkpoint: ShapeBenchmarkCheckpoint | None = None,
) -> ShapeBenchmarkReport:
    fingerprint = golden_fingerprint(examples)
    if checkpoint is not None and checkpoint.golden_fingerprint != fingerprint:
        raise ValueError("checkpoint golden fingerprint does not match the supplied examples")
    reports = tuple(
        ShapeModelReport(
            client_name,
            model,
            tuple(
                run_model(
                    examples,
                    client=client,
                    model=model,
                    completed=checkpoint.completed_for(model) if checkpoint else None,
                    on_example=(
                        (lambda report, _model=model: checkpoint.record(_model, report))
                        if checkpoint
                        else None
                    ),
                )
            ),
        )
        for model in models
    )
    return ShapeBenchmarkReport(
        client_name,
        seed,
        temperature,
        checkpoint.generated_at if checkpoint else datetime.now(timezone.utc).isoformat(),
        fingerprint,
        reports,
    )


def resolve_examples(filters: Sequence[str] | None) -> list[GoldenShapeExample]:
    if not filters:
        return list(GOLDEN_SHAPE_EXAMPLES)
    selected: list[GoldenShapeExample] = []
    for needle in filters:
        matches = [
            example for example in GOLDEN_SHAPE_EXAMPLES if needle.lower() in example.prompt.lower()
        ]
        if not matches:
            raise ValueError(f"no golden shape example matches {needle!r}")
        for example in matches:
            if example not in selected:
                selected.append(example)
    return selected


def resolve_models(client: LLMClient, models: Sequence[str] | None) -> list[str]:
    return list(models) if models else [client.default_model]


def build_client(
    client_name: str,
    *,
    base_url: str,
    timeout_seconds: float | None,
    seed: int,
    temperature: float,
) -> LLMClient:
    timeout = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
    if client_name == "claude-cli":
        return ClaudeCliClient(**timeout)
    if client_name == "google":
        return GoogleAIStudioClient(
            api_key=read_google_api_key(), temperature=temperature, **timeout
        )
    return OllamaClient(
        base_url=base_url, options={"seed": seed, "temperature": temperature}, **timeout
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shape_benchmark.py", description="Benchmark calendar-shape projection results."
    )
    parser.add_argument("--client", choices=["ollama", "claude-cli", "google"], default="google")
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        help="Repeatable model id; defaults to the selected client's default.",
    )
    parser.add_argument(
        "--example",
        dest="examples",
        action="append",
        help="Case-insensitive prompt substring filter.",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="Ollama only; Google runs record seed as unset."
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0, help="Applied by Ollama and Google."
    )
    parser.add_argument("--base-url", default=DEFAULT_OLLAMA_BASE_URL, help="Ollama only.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            f"Per-call timeout; defaults remain {DEFAULT_OLLAMA_TIMEOUT_SECONDS:g}s (ollama) "
            f"and {DEFAULT_GOOGLE_TIMEOUT_SECONDS:g}s (google)."
        ),
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="Write the final JSON report here."
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="Persist after each prompt and resume matching runs.",
    )
    return parser


def print_summary(report: ShapeBenchmarkReport) -> None:
    for model_report in report.models:
        print(f"\n=== {model_report.model} ({model_report.client}) ===")
        for example in model_report.examples:
            passed = sum(item.passed for item in example.expectations)
            print(
                f"  [{example.status.value:>10}] {example.prompt!r}: "
                f"{passed}/{len(example.expectations)} expectations"
            )
            if example.last_failure:
                print(f"      last: {example.last_failure.splitlines()[0]}")
            for item in example.expectations:
                if not item.passed:
                    print(f"      failed: {item.failure}")
        print(
            f"  {model_report.success_count}/{model_report.total} succeeded"
            f" | llm_calls={model_report.llm_calls}"
            f" input_tokens={model_report.input_tokens}"
            f" output_tokens={model_report.output_tokens}"
            f" cost_usd={model_report.cost_usd}"
            f" llm_duration_ms={model_report.llm_duration_ms}"
            f" wall_ms={model_report.wall_ms:.0f}"
        )


def any_call_error(report: ShapeBenchmarkReport) -> bool:
    return any(
        example.status is ShapeBenchmarkStatus.CALL_ERROR
        for model in report.models
        for example in model.examples
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        examples = resolve_examples(args.examples)
    except ValueError as exc:
        parser.error(str(exc))
    client = build_client(
        args.client,
        base_url=args.base_url,
        timeout_seconds=args.timeout,
        seed=args.seed,
        temperature=args.temperature,
    )
    seed = args.seed if args.client == "ollama" else None
    temperature = args.temperature if args.client in ("ollama", "google") else None
    checkpoint = None
    if args.checkpoint is not None:
        try:
            checkpoint = ShapeBenchmarkCheckpoint.start(
                args.checkpoint,
                client_name=args.client,
                seed=seed,
                temperature=temperature,
                examples=examples,
            )
        except ValueError as exc:
            parser.error(str(exc))
    report = run_benchmark(
        client_name=args.client,
        client=client,
        models=resolve_models(client, args.models),
        examples=examples,
        seed=seed,
        temperature=temperature,
        checkpoint=checkpoint,
    )
    print_summary(report)
    if args.output is not None:
        args.output.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"\nReport written to {args.output}")
    return 1 if any_call_error(report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
