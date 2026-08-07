"""Feed the five golden rule descriptions through the generation loop and report what happened.

**Invoked by hand, never from the test run.** It makes live calls against a real model backend —
an Ollama daemon by default, or the Claude CLI — and ``pyproject.toml``'s ``testpaths = ["tests"]``
is what keeps pytest from collecting it; ``rules/tests/test_benchmark.py`` covers report assembly
against synthetic data instead. Run it directly:

    python benchmark.py --client ollama --model qwen2.5:1.5b
    python benchmark.py --client ollama --model qwen2.5:1.5b --model llama3.1:8b
    python benchmark.py --client ollama --example weekends --example "max 1 hour"
    python benchmark.py --client ollama --model qwen2.5:1.5b --model llama3.1:8b \\
        --checkpoint bench.json   # safe to kill and re-run verbatim; resumes past what finished
    python benchmark.py --client google --model gemini-3.5-flash

**Token usage and cost come from a recording wrapper around the ``LLMClient``, not from a change
to ``run_generation_loop``.** The loop returns strings and discards every ``LLMResponse`` it saw —
correctly, since threading benchmark metadata out of it would give the engine's retry loop a
benchmark's concern to carry forever. ``RecordingClient`` is what recovers that metadata instead:
it sits at the same seam the loop already calls through, wrapping the real client for the duration
of one example and handing back everything it observed. It now lives in ``generation/llm.py``
rather than here — the backend's job runner needed the same recording, both directions this time,
to persist every prompt and completion a generation job makes (`.claude/rules/rule-engine.md`,
"Recording what was said to the model") — and this module imports it rather than keeping a second
copy. ``responses`` on it stays exactly the shape this module always used, so nothing below this
line changed beyond the import.

**An ``LLMCallError`` aborts the rest of that model's run, not just the example it hit.** The
loop's own docstring says the same error is not retried within one example: an unreachable daemon
or an unpulled model id is not fixed by a different prompt. The same reasoning applies one level up
— running four more examples to independently rediscover the same unreachable daemon burns minutes
of local CPU and buries the cause under repetitions of itself. The remaining examples for that
model are recorded as ``SKIPPED`` without running; a different ``--model`` still gets its own full
attempt, since one model's outage says nothing about another's.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from generation.errors import LLMCallError
from generation.llm import (
    DEFAULT_GOOGLE_TIMEOUT_SECONDS,
    DEFAULT_MODEL,
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
from generation.loop import MAX_RETRIES, LoopResult, run_generation_loop

__all__ = [
    "GOLDEN_EXAMPLES",
    "DEFAULT_OLLAMA_MODEL",
    "DEFAULT_GOOGLE_MODEL",
    "BenchmarkStatus",
    "ExampleReport",
    "ModelReport",
    "BenchmarkReport",
    "RecordingClient",
    "BenchmarkCheckpoint",
    "build_example_report",
    "run_model",
    "run_benchmark",
    "resolve_examples",
    "resolve_models",
    "build_client",
    "build_arg_parser",
    "print_summary",
    "any_call_error",
    "main",
]

#: The golden set, in this order. Fixed rather than configurable: a benchmark that measured a
#: different set of constraints each run could not compare one prompt revision against the next.
GOLDEN_EXAMPLES: tuple[str, ...] = (
    "max 1 hour",
    "only on weekends",
    "max 2 times a week",
    "no more than 3 hours total per day",
    "at most 2 bookings on the same day",
)

#: A model small enough to be a reasonable first thing to try on a laptop CPU. Not a claim it
#: holds the contract — that is what running this benchmark against it is for.
DEFAULT_OLLAMA_MODEL = "qwen2.5:1.5b"

#: GA rather than a floating ``-latest`` alias — a benchmark whose model id can drift out from
#: under it cannot compare one run against the next. Unlike the ollama default above, this one
#: *is* a claim that the model holds the contract: it took all five golden examples on the first
#: attempt, in ten calls and 26 seconds (``rule-engine.md``). It is also the tier the free key can
#: actually finish a run on — the flagship ``gemini-3.x-flash`` models cap at 20 requests per day
#: per model there, below what a five-example run costs when anything retries.
DEFAULT_GOOGLE_MODEL = "gemini-3.1-flash-lite"


class BenchmarkStatus(str, Enum):
    """How one example's run ended. Matches ``AttemptOutcome``'s shape: a small ``str`` enum."""

    #: The loop produced a verified candidate.
    VERIFIED = "verified"
    #: The loop exhausted its retries without one.
    GAVE_UP = "gave_up"
    #: The model backend could not be reached or answered with something unusable.
    CALL_ERROR = "call_error"
    #: Not run at all, because an earlier example already hit ``CALL_ERROR`` for this model.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ExampleReport:
    """One golden example, against one model: what happened and what it cost.

    ``outcomes`` is the ordered sequence of each attempt's ``AttemptOutcome`` value — the point of
    this task. ``RULE_REJECTED`` on attempt 1 and ``TESTS_FAILED`` on attempt 1 are different
    findings about which constraint in the system prompt a model broke, and collapsing them into a
    single success/failure bit is exactly the information a success-rate number erases.
    """

    description: str
    model: str
    status: BenchmarkStatus
    succeeded: bool
    attempts: int
    retries: int
    outcomes: tuple[str, ...]
    llm_calls: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    llm_duration_ms: int | None
    wall_ms: float
    last_failure: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "model": self.model,
            "status": self.status.value,
            "succeeded": self.succeeded,
            "attempts": self.attempts,
            "retries": self.retries,
            "outcomes": list(self.outcomes),
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "llm_duration_ms": self.llm_duration_ms,
            "wall_ms": self.wall_ms,
            "last_failure": self.last_failure,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExampleReport":
        """The inverse of ``to_dict``, for reading a checkpoint back off disk.

        Every field round-trips through JSON unchanged except ``status`` (a string in the file,
        the ``BenchmarkStatus`` enum here) and ``outcomes`` (a list in the file, a tuple here) —
        the same two conversions ``to_dict`` made going the other way.
        """
        return cls(
            description=data["description"],
            model=data["model"],
            status=BenchmarkStatus(data["status"]),
            succeeded=data["succeeded"],
            attempts=data["attempts"],
            retries=data["retries"],
            outcomes=tuple(data["outcomes"]),
            llm_calls=data["llm_calls"],
            input_tokens=data["input_tokens"],
            output_tokens=data["output_tokens"],
            cost_usd=data["cost_usd"],
            llm_duration_ms=data["llm_duration_ms"],
            wall_ms=data["wall_ms"],
            last_failure=data["last_failure"],
        )


@dataclass(frozen=True)
class ModelReport:
    """Every golden example run against one model, plus the totals across them."""

    client: str
    model: str
    examples: tuple[ExampleReport, ...]

    @property
    def total(self) -> int:
        return len(self.examples)

    @property
    def success_count(self) -> int:
        return sum(1 for example in self.examples if example.succeeded)

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
class BenchmarkReport:
    """One whole run: the parameters it was made with, and every model's results.

    The run parameters are recorded alongside the results because a report that does not say how
    it was produced cannot be compared with the next one — the same reasoning ``--seed`` and
    ``--temperature`` exist for in the first place.

    ``seed`` and ``temperature`` are ``None`` for a client that does not take them. Recording the
    flag's default value for a run that never applied it would be worse than recording nothing: two
    reports would agree on a sampling parameter neither of them set, and a difference between them
    would be attributed to anything but the reason.
    """

    client: str
    seed: int | None
    temperature: float | None
    retries: int
    generated_at: str
    models: tuple[ModelReport, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "seed": self.seed,
            "temperature": self.temperature,
            "retries": self.retries,
            "generated_at": self.generated_at,
            "models": [model.to_dict() for model in self.models],
        }


@dataclass
class BenchmarkCheckpoint:
    """Persists a run's ``ExampleReport``s to ``path`` after every example, so a run killed partway
    — a session limit, a laptop sleeping, a `^C` — resumes from its last completed example on the
    next invocation instead of repeating (and re-billing, for a paid client) every example already
    settled.

    The file at ``path`` is a valid ``BenchmarkReport.to_dict()`` at every point, partial or
    complete — the same shape ``--output`` writes — which is what lets ``start`` read one back.
    """

    path: Path
    client_name: str
    seed: int | None
    temperature: float | None
    retries: int
    generated_at: str
    _completed: dict[str, dict[str, ExampleReport]] = field(default_factory=dict)

    @classmethod
    def start(
        cls,
        path: Path,
        *,
        client_name: str,
        seed: int | None,
        temperature: float | None,
        retries: int,
    ) -> "BenchmarkCheckpoint":
        """Resume from ``path`` if it holds a checkpoint for these exact run parameters; start a
        fresh one if ``path`` does not exist yet.

        A checkpoint written under a different client, seed, temperature or retry count is not
        safe to resume into silently: the examples already on disk and the ones about to run would
        not have been produced under comparable conditions, which is the same reasoning
        ``BenchmarkReport`` records its own parameters for. Raise rather than guess which half of
        a mismatched pair to trust.
        """
        if not path.exists():
            return cls(
                path=path,
                client_name=client_name,
                seed=seed,
                temperature=temperature,
                retries=retries,
                generated_at=datetime.now(timezone.utc).isoformat(),
            )

        data = json.loads(path.read_text(encoding="utf-8"))
        found = (data["client"], data["seed"], data["temperature"], data["retries"])
        wanted = (client_name, seed, temperature, retries)
        if found != wanted:
            raise ValueError(
                f"checkpoint at {path} was started with client={data['client']!r} "
                f"seed={data['seed']!r} temperature={data['temperature']!r} "
                f"retries={data['retries']!r}, which does not match this run's "
                f"client={client_name!r} seed={seed!r} temperature={temperature!r} "
                f"retries={retries!r}. Use a different --checkpoint path, or delete the existing "
                "file to start that run over."
            )

        completed = {
            model_report["model"]: {
                example["description"]: ExampleReport.from_dict(example)
                for example in model_report["examples"]
            }
            for model_report in data["models"]
        }
        return cls(
            path=path,
            client_name=client_name,
            seed=seed,
            temperature=temperature,
            retries=retries,
            generated_at=data["generated_at"],
            _completed=completed,
        )

    def completed_for(self, model: str) -> dict[str, ExampleReport]:
        """Every example already recorded for ``model``, keyed by description. Empty for a model
        this checkpoint has never seen — including one added to ``--model`` after the checkpoint
        was started, which simply runs in full."""
        return self._completed.get(model, {})

    def record(self, model: str, example: ExampleReport) -> None:
        """Record one finished example for ``model`` and flush the whole checkpoint to disk.

        Flushing after every single example, rather than batching, is the point of this class: the
        file on disk is never more than one example stale, so a kill at any moment loses at most
        the example that was in flight.
        """
        self._completed.setdefault(model, {})[example.description] = example
        self.path.write_text(json.dumps(self._report().to_dict(), indent=2), encoding="utf-8")

    def _report(self) -> BenchmarkReport:
        models = tuple(
            ModelReport(client=self.client_name, model=model, examples=tuple(examples.values()))
            for model, examples in self._completed.items()
        )
        return BenchmarkReport(
            client=self.client_name,
            seed=self.seed,
            temperature=self.temperature,
            retries=self.retries,
            generated_at=self.generated_at,
            models=models,
        )


def _sum_optional(values: Iterable[int | float | None]) -> int | float | None:
    """Sum the values that are present; ``None``, not ``0``, if none of them are.

    Every ``LLMResponse`` metadata field is optional, and a backend that cannot report a number
    leaves it ``None`` rather than a fabricated zero — ``OllamaClient.cost_usd`` is the clearest
    case, but the rule is general. Folding a run of all-``None`` values into ``0`` here would
    republish that fabrication one layer up.
    """
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def build_example_report(
    description: str,
    *,
    model: str,
    result: LoopResult,
    responses: Sequence[LLMResponse],
    wall_ms: float,
) -> ExampleReport:
    """Assemble an ``ExampleReport`` from a finished ``LoopResult`` and the calls it made.

    Pure: no client, no clock beyond the ``wall_ms`` already measured by the caller. This is the
    function the unit tests exercise — nothing in the test suite runs a loop or calls a model.
    """
    return ExampleReport(
        description=description,
        model=model,
        status=BenchmarkStatus.VERIFIED if result.succeeded else BenchmarkStatus.GAVE_UP,
        succeeded=result.succeeded,
        attempts=result.attempt_count,
        retries=result.retries,
        outcomes=tuple(attempt.outcome.value for attempt in result.attempts),
        llm_calls=len(responses),
        input_tokens=_sum_optional(response.input_tokens for response in responses),
        output_tokens=_sum_optional(response.output_tokens for response in responses),
        cost_usd=_sum_optional(response.cost_usd for response in responses),
        llm_duration_ms=_sum_optional(response.duration_ms for response in responses),
        wall_ms=wall_ms,
        last_failure=result.last_failure,
    )


def _call_error_report(
    description: str, *, model: str, detail: str, wall_ms: float
) -> ExampleReport:
    return ExampleReport(
        description=description,
        model=model,
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
        wall_ms=wall_ms,
        last_failure=detail,
    )


def _skipped_report(description: str, *, model: str) -> ExampleReport:
    return ExampleReport(
        description=description,
        model=model,
        status=BenchmarkStatus.SKIPPED,
        succeeded=False,
        attempts=0,
        retries=0,
        outcomes=(),
        llm_calls=0,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        llm_duration_ms=None,
        wall_ms=0.0,
        last_failure="skipped: an earlier example hit an LLMCallError and aborted this model's run",
    )


def run_model(
    descriptions: Sequence[str],
    *,
    client: LLMClient,
    model: str,
    retries: int,
    completed: Mapping[str, ExampleReport] | None = None,
    on_example: Callable[[ExampleReport], None] | None = None,
) -> list[ExampleReport]:
    """Run every description in ``descriptions`` against ``model``, one loop per description.

    ``output_dir=None`` on every call: a benchmark is not a source of artifacts for a human to
    review, and five examples times several models writing candidate directories would bury the
    real generation-loop output under a run that was never meant to produce any.

    ``completed`` is this model's already-recorded examples, keyed by description — a resumed
    run's checkpoint, or empty for a fresh one. A description found there, including a previous
    ``SKIPPED`` one, is reused as-is and never re-run; a ``CALL_ERROR`` found there re-arms
    ``aborted`` exactly as hitting one fresh would, so a resumed run does not retry a model whose
    daemon was unreachable last time by way of running every remaining example again first.

    ``on_example`` is called with each freshly-computed report — never with one reused from
    ``completed``, which is already persisted — immediately after it is produced, so a caller can
    checkpoint progress before moving to the next description.
    """
    reports: list[ExampleReport] = []
    aborted = False
    completed = completed or {}

    for description in descriptions:
        cached = completed.get(description)
        if cached is not None:
            reports.append(cached)
            if cached.status is BenchmarkStatus.CALL_ERROR:
                aborted = True
            continue

        if aborted:
            report = _skipped_report(description, model=model)
        else:
            recording = RecordingClient(client)
            started = time.perf_counter()
            try:
                result = run_generation_loop(
                    description,
                    client=recording,
                    model=model,
                    max_retries=retries,
                    output_dir=None,
                )
            except LLMCallError as exc:
                wall_ms = (time.perf_counter() - started) * 1000
                report = _call_error_report(
                    description, model=model, detail=str(exc), wall_ms=wall_ms
                )
                aborted = True
            else:
                wall_ms = (time.perf_counter() - started) * 1000
                report = build_example_report(
                    description,
                    model=model,
                    result=result,
                    responses=recording.responses,
                    wall_ms=wall_ms,
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
    descriptions: Sequence[str],
    retries: int,
    seed: int | None,
    temperature: float | None,
    checkpoint: BenchmarkCheckpoint | None = None,
) -> BenchmarkReport:
    """Run every model over every description and assemble the whole report.

    One model erroring out does not touch another: each gets its own call to ``run_model`` and its
    own fresh abort state, so a daemon that has ``llama3.1:8b`` pulled but not ``qwen2.5:14b`` still
    reports a full run for the one it has.

    With a ``checkpoint``, each model's already-recorded examples are reused rather than re-run,
    and every freshly-computed one is handed to ``checkpoint.record`` as it finishes — so a run
    resumed from a checkpoint costs nothing beyond re-checking work already done, and one killed
    partway through has lost at most its one in-flight example.
    """
    model_reports = tuple(
        ModelReport(
            client=client_name,
            model=model,
            examples=tuple(
                run_model(
                    descriptions,
                    client=client,
                    model=model,
                    retries=retries,
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
    return BenchmarkReport(
        client=client_name,
        seed=seed,
        temperature=temperature,
        retries=retries,
        generated_at=(
            checkpoint.generated_at if checkpoint else datetime.now(timezone.utc).isoformat()
        ),
        models=model_reports,
    )


def resolve_examples(filters: Sequence[str] | None) -> list[str]:
    """The golden descriptions to run: all five, or those matching every ``--example`` filter.

    An unmatched filter raises rather than silently running nothing — a prompt-tuning session
    re-running one example against a slow local model wants to know it mistyped the filter, not to
    read a report with a mysteriously absent example in it.
    """
    if not filters:
        return list(GOLDEN_EXAMPLES)

    selected: list[str] = []
    for needle in filters:
        matches = [
            description for description in GOLDEN_EXAMPLES if needle.lower() in description.lower()
        ]
        if not matches:
            available = ", ".join(repr(description) for description in GOLDEN_EXAMPLES)
            raise ValueError(f"no golden example matches {needle!r}. Available: {available}")
        for match in matches:
            if match not in selected:
                selected.append(match)
    return selected


def resolve_models(client_name: str, models: Sequence[str] | None) -> list[str]:
    """The models to run: what ``--model`` named, or one client-appropriate default."""
    if models:
        return list(models)
    if client_name == "claude-cli":
        return [DEFAULT_MODEL]
    if client_name == "google":
        return [DEFAULT_GOOGLE_MODEL]
    return [DEFAULT_OLLAMA_MODEL]


def build_client(
    client_name: str,
    *,
    base_url: str,
    timeout_seconds: float | None,
    seed: int,
    temperature: float,
) -> LLMClient:
    """One client for the whole run. ``model`` is a per-call argument, not a constructor one, so
    the same instance serves every ``--model`` without being rebuilt.

    ``base_url`` is Ollama's own flag and is not reused for ``google``: AI Studio is a fixed
    hosted endpoint, not a local daemon a developer points at a different address.

    ``timeout_seconds`` is *not* in that category, and treating it as one made a model
    unmeasurable. Every client here bounds a call with a wall clock, and a thinking-heavy hosted
    model can legitimately need longer than a hosted client's own default — a measured
    ``gemini-3-flash-preview`` run spent all 120 of its default seconds on a single generation and
    was recorded as an unreachable backend, which says nothing true about whether that model holds
    the rule contract. So ``--timeout`` applies to whichever client is selected, and ``None``
    means "leave that client's own default alone" rather than a number this function invents for
    all three.
    """
    timeout = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
    if client_name == "claude-cli":
        return ClaudeCliClient(**timeout)
    if client_name == "google":
        return GoogleAIStudioClient(
            api_key=read_google_api_key(), temperature=temperature, **timeout
        )
    return OllamaClient(
        base_url=base_url,
        options={"seed": seed, "temperature": temperature},
        **timeout,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmark.py",
        description=(
            "Feed the golden rule descriptions through the generation loop and report the "
            "per-attempt outcomes, token usage, cost and latency for one or more models."
        ),
    )
    parser.add_argument(
        "--client",
        choices=["ollama", "claude-cli", "google"],
        default="ollama",
        help="Which LLMClient backend to benchmark. Default: ollama.",
    )
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        help=(
            "Model id to benchmark. Repeatable, to compare models side by side in one run. "
            "Defaults to a single client-appropriate model."
        ),
    )
    parser.add_argument(
        "--example",
        dest="examples",
        action="append",
        help=(
            "Case-insensitive substring filter over the golden descriptions. Repeatable. "
            "Defaults to all five. An unmatched filter is an error."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help=(
            "Passed to the ollama client's options.seed, so two runs are comparable. Ollama "
            "client only: AI Studio's generationConfig accepts a seed field but does not honour "
            "it — confirmed against the live API, where an identical seed and temperature "
            "returned different completions — so a google run records this as unset rather than "
            "claim a value it did not apply. Default: 0."
        ),
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help=(
            "Passed to the ollama client's options.temperature and to the google client's "
            "generationConfig.temperature — both clients apply it, unlike --seed. Default: 0.0."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help=f"Ollama daemon base URL. Ollama client only. Default: {DEFAULT_OLLAMA_BASE_URL}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Wall clock per model call, in seconds, for whichever client is selected. Unset "
            "leaves that client's own default in place — "
            f"{DEFAULT_OLLAMA_TIMEOUT_SECONDS:g}s for ollama, "
            f"{DEFAULT_GOOGLE_TIMEOUT_SECONDS:g}s for google. Raise it for a thinking-heavy "
            "hosted model, whose single generation can outlast the hosted default and be "
            "recorded as an unreachable backend rather than as anything about the model."
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=MAX_RETRIES,
        help=f"Retries per example, passed to the generation loop. Default: {MAX_RETRIES}.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the JSON report to this path. The terminal summary always prints regardless.",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help=(
            "Persist progress to this path after every example, and resume from it if it already "
            "exists — makes a long multi-model run safe to kill and re-invoke with the same "
            "arguments. --client/--seed/--temperature/--retries must match the checkpoint's own "
            "run, or it refuses to resume rather than mix incomparable results."
        ),
    )
    return parser


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


def print_summary(report: BenchmarkReport) -> None:
    """The terminal summary: a per-example line with its outcome sequence, then per-model totals.

    Reads the same objects the JSON report is built from, so the two never diverge in what they
    say happened.
    """
    for model_report in report.models:
        print(f"\n=== {model_report.model} ({model_report.client}) ===")
        for example in model_report.examples:
            outcome_seq = " -> ".join(example.outcomes) if example.outcomes else "(none)"
            print(f"  [{example.status.value:>10}] {example.description!r}: {outcome_seq}")
            if not example.succeeded and example.last_failure:
                print(f"      last: {_first_line(example.last_failure)}")
        print(
            f"  {model_report.success_count}/{model_report.total} succeeded"
            f" | llm_calls={model_report.llm_calls}"
            f" input_tokens={model_report.input_tokens}"
            f" output_tokens={model_report.output_tokens}"
            f" cost_usd={model_report.cost_usd}"
            f" llm_duration_ms={model_report.llm_duration_ms}"
            f" wall_ms={model_report.wall_ms:.0f}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        descriptions = resolve_examples(args.examples)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover - parser.error always raises SystemExit first

    models = resolve_models(args.client, args.models)
    client = build_client(
        args.client,
        base_url=args.base_url,
        timeout_seconds=args.timeout,
        seed=args.seed,
        temperature=args.temperature,
    )
    # Each sampling parameter is recorded only for a run that actually applied it — two reports
    # agreeing on a parameter neither of them set is worse than two reports that say nothing
    # about it. The two parameters no longer share one gate: only ollama is ever handed a seed,
    # but both ollama and google apply temperature — google accepts a seed field in
    # generationConfig without honouring it (GoogleAIStudioClient's docstring), so a google run
    # must not claim one either, while it may honestly claim the temperature it was given.
    seed = args.seed if args.client == "ollama" else None
    temperature = args.temperature if args.client in ("ollama", "google") else None

    checkpoint = None
    if args.checkpoint is not None:
        try:
            checkpoint = BenchmarkCheckpoint.start(
                args.checkpoint,
                client_name=args.client,
                seed=seed,
                temperature=temperature,
                retries=args.retries,
            )
        except ValueError as exc:
            parser.error(str(exc))
            return 2  # pragma: no cover - parser.error always raises SystemExit first

    report = run_benchmark(
        client_name=args.client,
        client=client,
        models=models,
        descriptions=descriptions,
        retries=args.retries,
        seed=seed,
        temperature=temperature,
        checkpoint=checkpoint,
    )

    print_summary(report)

    if args.output is not None:
        args.output.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"\nReport written to {args.output}")

    # A model that gave up is a *result* — the question this benchmark exists to answer is whether
    # a given model can hold the rule contract at all, and "no" is an answer, so it exits 0. A
    # backend that could not be reached is not a result at all, and exiting 0 on it would make an
    # unreachable daemon look like a run whose numbers mean something.
    return 1 if any_call_error(report) else 0


def any_call_error(report: BenchmarkReport) -> bool:
    return any(
        example.status is BenchmarkStatus.CALL_ERROR
        for model_report in report.models
        for example in model_report.examples
    )


if __name__ == "__main__":
    raise SystemExit(main())
