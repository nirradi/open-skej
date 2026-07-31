"""Feed the five golden rule descriptions through the generation loop and report what happened.

**Invoked by hand, never from the test run.** It makes live calls against a real model backend —
an Ollama daemon by default, or the Claude CLI — and ``pyproject.toml``'s ``testpaths = ["tests"]``
is what keeps pytest from collecting it; ``rules/tests/test_benchmark.py`` covers report assembly
against synthetic data instead. Run it directly:

    python benchmark.py --client ollama --model qwen2.5:1.5b
    python benchmark.py --client ollama --model qwen2.5:1.5b --model llama3.1:8b
    python benchmark.py --client ollama --example weekends --example "max 1 hour"

**Token usage and cost come from a recording wrapper around the ``LLMClient``, not from a change
to ``run_generation_loop``.** The loop returns strings and discards every ``LLMResponse`` it saw —
correctly, since threading benchmark metadata out of it would give the engine's retry loop a
benchmark's concern to carry forever. ``RecordingClient`` is what recovers that metadata instead:
it sits at the same seam the loop already calls through, wrapping the real client for the duration
of one example and handing back everything it observed.

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
from typing import Any, Iterable, Sequence

from generation.errors import LLMCallError
from generation.llm import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    ClaudeCliClient,
    LLMClient,
    LLMResponse,
    OllamaClient,
)
from generation.loop import MAX_RETRIES, LoopResult, run_generation_loop

__all__ = [
    "GOLDEN_EXAMPLES",
    "DEFAULT_OLLAMA_MODEL",
    "BenchmarkStatus",
    "ExampleReport",
    "ModelReport",
    "BenchmarkReport",
    "RecordingClient",
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

#: Verbatim, in this order — the five constraints item 10 and task 6.2 name.
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


@dataclass
class RecordingClient:
    """Wraps an ``LLMClient`` and remembers every ``LLMResponse`` it returned.

    This is the whole reason the seam in ``llm.py`` exists in this shape: the loop calls
    ``client.complete`` and nothing else, so anything that also implements ``complete`` can sit
    between the loop and the real backend without the loop knowing the difference. Construct one
    per example — a fresh ``responses`` list — wrapping the same underlying client, and read
    ``responses`` back once the loop returns.
    """

    wrapped: LLMClient
    responses: list[LLMResponse] = field(default_factory=list)

    def complete(self, *, system: str, prompt: str, model: str = DEFAULT_MODEL) -> LLMResponse:
        response = self.wrapped.complete(system=system, prompt=prompt, model=model)
        self.responses.append(response)
        return response


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
) -> list[ExampleReport]:
    """Run every description in ``descriptions`` against ``model``, one loop per description.

    ``output_dir=None`` on every call: a benchmark is not a source of artifacts for a human to
    review, and five examples times several models writing candidate directories would bury the
    real generation-loop output under a run that was never meant to produce any.
    """
    reports: list[ExampleReport] = []
    aborted = False

    for description in descriptions:
        if aborted:
            reports.append(_skipped_report(description, model=model))
            continue

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
            reports.append(
                _call_error_report(description, model=model, detail=str(exc), wall_ms=wall_ms)
            )
            aborted = True
            continue

        wall_ms = (time.perf_counter() - started) * 1000
        reports.append(
            build_example_report(
                description,
                model=model,
                result=result,
                responses=recording.responses,
                wall_ms=wall_ms,
            )
        )

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
) -> BenchmarkReport:
    """Run every model over every description and assemble the whole report.

    One model erroring out does not touch another: each gets its own call to ``run_model`` and its
    own fresh abort state, so a daemon that has ``llama3.1:8b`` pulled but not ``qwen2.5:14b`` still
    reports a full run for the one it has.
    """
    model_reports = tuple(
        ModelReport(
            client=client_name,
            model=model,
            examples=tuple(run_model(descriptions, client=client, model=model, retries=retries)),
        )
        for model in models
    )
    return BenchmarkReport(
        client=client_name,
        seed=seed,
        temperature=temperature,
        retries=retries,
        generated_at=datetime.now(timezone.utc).isoformat(),
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
    return [DEFAULT_MODEL] if client_name == "claude-cli" else [DEFAULT_OLLAMA_MODEL]


def build_client(
    client_name: str,
    *,
    base_url: str,
    timeout_seconds: float,
    seed: int,
    temperature: float,
) -> LLMClient:
    """One client for the whole run. ``model`` is a per-call argument, not a constructor one, so
    the same instance serves every ``--model`` without being rebuilt."""
    if client_name == "claude-cli":
        return ClaudeCliClient()
    return OllamaClient(
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        options={"seed": seed, "temperature": temperature},
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
        choices=["ollama", "claude-cli"],
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
            "Passed to the ollama client's options.seed, so two runs are comparable. "
            "Ollama client only. Default: 0."
        ),
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help=(
            "Passed to the ollama client's options.temperature. Ollama client only. Default: 0.0."
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
        default=DEFAULT_OLLAMA_TIMEOUT_SECONDS,
        help=(
            "Wall clock per ollama call, in seconds. Ollama client only. "
            f"Default: {DEFAULT_OLLAMA_TIMEOUT_SECONDS:g}."
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
    # Only the ollama client is handed these, so only an ollama run may claim them.
    sampling_applies = args.client == "ollama"

    report = run_benchmark(
        client_name=args.client,
        client=client,
        models=models,
        descriptions=descriptions,
        retries=args.retries,
        seed=args.seed if sampling_applies else None,
        temperature=args.temperature if sampling_applies else None,
    )

    print_summary(report)

    if args.output is not None:
        args.output.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"\nReport written to {args.output}")

    # A model that gave up is a *result* — item 10's whole question is whether a small model can
    # hold the contract, and "no" is an answer this exits 0 on. A backend that could not be reached
    # is not a result at all, and exiting 0 on it would make an unreachable daemon look like a run
    # whose numbers mean something.
    return 1 if any_call_error(report) else 0


def any_call_error(report: BenchmarkReport) -> bool:
    return any(
        example.status is BenchmarkStatus.CALL_ERROR
        for model_report in report.models
        for example in model_report.examples
    )


if __name__ == "__main__":
    raise SystemExit(main())
