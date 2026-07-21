"""The retry loop: generate a rule, have it attacked, and keep the one that survives.

One attempt is: the Generator writes a rule, the Tester writes an adversarial suite against *that*
rule, and the sandbox runs the suite. If everything passes, the candidate and its tests are written
to ``rules/generated/`` for a human to read. If anything else happens, the failure goes back to the
Generator and the loop tries again, **at most three times**.

**Fail closed, and this is where it is easiest to get wrong.** Only ``SandboxOutcome.PASSED``
advances a candidate. A timeout is not a success and a crash is not a success — both mean the suite
never delivered a verdict, and a candidate nobody could verify is exactly the candidate that must
not reach the canon. The check is ``result.outcome is SandboxOutcome.PASSED`` rather than
"not FAILED", because the second admits the two outcomes that establish nothing.

**Nothing here is ever imported.** ``generated/`` is an output directory, gitignored, holding a file
a developer reads and, if they agree with it, ports into the canon by hand through a PR. That is
what keeps a prompt injection in a rule description out of the request path: there is no code path
from this module to anything the booking API runs.

**The tests are rewritten on every attempt, never reused.** A suite imports the rule class by name
and constructs it with its own parameters, so a suite kept from the previous attempt would fail
against a renamed class or a changed signature and report it as the rule being wrong. The cost is a
second model call per retry; the alternative is feedback that sends the Generator to fix the wrong
thing.

An ``LLMCallError`` is not retried. The backend being unreachable, unauthenticated or pointed at a
model id that does not exist is not something another prompt fixes, and burning the budget on it
would bury the real cause under three identical failures. It propagates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from rules.sandbox import (
    DEFAULT_MEMORY_LIMIT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    SandboxOutcome,
    SandboxResult,
)

from .errors import RuleRejectedError, SuiteRejectedError
from .generator import generate_rule
from .harness import run_candidate
from .llm import DEFAULT_MODEL, LLMClient
from .tester import generate_tests

__all__ = [
    "run_generation_loop",
    "LoopResult",
    "Attempt",
    "AttemptOutcome",
    "MAX_RETRIES",
    "DEFAULT_OUTPUT_DIR",
    "FAILURE_EXCERPT_CHARS",
    "write_artifact",
]

#: Retries after the first attempt, so a run makes at most ``MAX_RETRIES + 1`` attempts. Three is
#: from the stream brief. It is a budget, not a target: a rule that is still wrong on the fourth
#: attempt is usually a description the model cannot act on rather than one it keeps mistyping, and
#: further retries spend real money re-reading the same misunderstanding.
MAX_RETRIES = 3

#: Where a verified candidate lands. Sibling of ``generation/`` and ``rules/``, and gitignored:
#: what is in here is a draft for review, and a draft committed by accident is a draft that looks
#: like it was accepted.
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "generated"

#: How much of a failure report is fed back to the model. The *head* is kept, not the tail: pytest
#: reports failures in the order they ran and the first one is the one to fix, while the tail of a
#: long run is a summary list naming tests whose detail has already been cut.
FAILURE_EXCERPT_CHARS = 6000


class AttemptOutcome(str, Enum):
    """How one attempt ended. Exactly one of these means the candidate is verified."""

    PASSED = "passed"
    #: The Generator's source did not survive the safety validator.
    RULE_REJECTED = "rule_rejected"
    #: The Tester's answer was not a usable pytest module.
    TESTS_REJECTED = "tests_rejected"
    #: The suite ran and the rule got something wrong. The informative failure.
    TESTS_FAILED = "tests_failed"
    #: The suite never finished, or never ran. Not a verdict, and not a success.
    TIMEOUT = "timeout"
    CRASHED = "crashed"


@dataclass(frozen=True)
class Attempt:
    """One pass through generate → test → run, and what became of it."""

    number: int
    outcome: AttemptOutcome
    rule_source: str | None = None
    test_source: str | None = None
    #: What is fed back to the Generator next time; ``None`` only when the attempt succeeded.
    failure: str | None = None
    sandbox: SandboxResult | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is AttemptOutcome.PASSED


@dataclass(frozen=True)
class LoopResult:
    """The outcome of a whole run, with every attempt it took kept for inspection.

    ``rule_source`` is populated **only** on success. A caller cannot reach for the source of a
    candidate that was never verified without going through ``attempts`` and deciding deliberately
    that it wants an unverified one.
    """

    description: str
    succeeded: bool
    attempts: tuple[Attempt, ...] = ()
    rule_source: str | None = None
    test_source: str | None = None
    artifact_path: Path | None = None
    model: str = DEFAULT_MODEL

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def retries(self) -> int:
        """Attempts beyond the first. Zero when the first candidate passed."""
        return max(0, len(self.attempts) - 1)

    @property
    def last_failure(self) -> str | None:
        """Why the last attempt did not work, or ``None`` if it did."""
        return self.attempts[-1].failure if self.attempts else None

    def summary(self) -> str:
        """One line for a terminal. Never shown to a booking user."""
        attempts = f"{self.attempt_count} attempt{'' if self.attempt_count == 1 else 's'}"
        if self.succeeded:
            where = f" → {self.artifact_path}" if self.artifact_path else ""
            return f"verified after {attempts}{where}"
        return f"gave up after {attempts} ({self.retries} retries); last: {self.last_failure}"


def run_generation_loop(
    description: str,
    *,
    client: LLMClient,
    model: str = DEFAULT_MODEL,
    max_retries: int = MAX_RETRIES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
    output_dir: Path | None = DEFAULT_OUTPUT_DIR,
) -> LoopResult:
    """Generate a rule for ``description`` and verify it, retrying up to ``max_retries`` times.

    Returns a ``LoopResult`` either way: giving up is a normal outcome of a generation run and not
    an exception. ``LLMCallError`` does propagate — see the module docstring.

    ``output_dir=None`` runs the loop without writing anything, which is what the benchmark wants
    and what most tests want.
    """
    if not description or not description.strip():
        raise ValueError("description must be a non-empty rule description")
    if max_retries < 0:
        raise ValueError(f"max_retries must not be negative, got {max_retries!r}")

    attempts: list[Attempt] = []
    previous_source: str | None = None
    failure: str | None = None

    for number in range(1, max_retries + 2):
        attempt = _attempt(
            description,
            number=number,
            client=client,
            model=model,
            previous_source=previous_source,
            failure=failure,
            timeout_seconds=timeout_seconds,
            memory_limit_bytes=memory_limit_bytes,
        )
        attempts.append(attempt)

        if attempt.succeeded:
            artifact = None
            if output_dir is not None:
                artifact = write_artifact(
                    description,
                    rule_source=attempt.rule_source or "",
                    test_source=attempt.test_source or "",
                    output_dir=output_dir,
                )
            return LoopResult(
                description=description,
                succeeded=True,
                attempts=tuple(attempts),
                rule_source=attempt.rule_source,
                test_source=attempt.test_source,
                artifact_path=artifact,
                model=model,
            )

        # Carry the failing candidate forward so the next call is a correction rather than an
        # unrelated second guess. When the rule itself was rejected there is still source to show:
        # RuleRejectedError carries what the model said.
        previous_source = attempt.rule_source
        failure = attempt.failure

    # Budget exhausted. Nothing is written: an unverified candidate does not become an artifact,
    # which is the same rule the sandbox applies one level down.
    return LoopResult(
        description=description,
        succeeded=False,
        attempts=tuple(attempts),
        model=model,
    )


def _attempt(
    description: str,
    *,
    number: int,
    client: LLMClient,
    model: str,
    previous_source: str | None,
    failure: str | None,
    timeout_seconds: float,
    memory_limit_bytes: int,
) -> Attempt:
    """One generate → test → run cycle, classified. Never raises for candidate misbehaviour."""
    try:
        rule_source = generate_rule(
            description,
            client=client,
            model=model,
            previous_source=previous_source,
            failure=failure,
        )
    except RuleRejectedError as exc:
        return Attempt(
            number=number,
            outcome=AttemptOutcome.RULE_REJECTED,
            rule_source=exc.source,
            failure=f"The safety validator rejected the source: {exc.reason}",
        )

    try:
        test_source = generate_tests(rule_source, description, client=client, model=model)
    except SuiteRejectedError as exc:
        # The rule is not implicated: nothing ran. It is carried forward anyway so the next attempt
        # is still a correction, and the failure text says whose fault this was.
        return Attempt(
            number=number,
            outcome=AttemptOutcome.TESTS_REJECTED,
            rule_source=rule_source,
            test_source=exc.source,
            failure=(
                "No tests could be written for this rule, so it could not be verified: "
                f"{exc.reason}"
            ),
        )

    result = run_candidate(
        rule_source,
        test_source,
        timeout_seconds=timeout_seconds,
        memory_limit_bytes=memory_limit_bytes,
    )

    if result.outcome is SandboxOutcome.PASSED:
        return Attempt(
            number=number,
            outcome=AttemptOutcome.PASSED,
            rule_source=rule_source,
            test_source=test_source,
            sandbox=result,
        )

    return Attempt(
        number=number,
        outcome=_SANDBOX_TO_ATTEMPT[result.outcome],
        rule_source=rule_source,
        test_source=test_source,
        failure=describe_failure(result),
        sandbox=result,
    )


#: Every non-passing sandbox outcome maps to a non-passing attempt outcome. Written as a total map
#: rather than an if-chain with an ``else: PASSED`` fallthrough, which is the shape that lets an
#: outcome added later be read as a success by default.
_SANDBOX_TO_ATTEMPT = {
    SandboxOutcome.FAILED: AttemptOutcome.TESTS_FAILED,
    SandboxOutcome.TIMEOUT: AttemptOutcome.TIMEOUT,
    SandboxOutcome.CRASHED: AttemptOutcome.CRASHED,
}


def describe_failure(result: SandboxResult, *, limit: int = FAILURE_EXCERPT_CHARS) -> str:
    """Turn a sandbox result into the text handed back to the Generator.

    A timeout gets a sentence rather than output, because there usually is none worth reading and
    the diagnosis is not in it: the rule did not finish, which for a rule doing date arithmetic over
    a capped history window means it is looping.
    """
    if result.outcome is SandboxOutcome.TIMEOUT:
        return (
            f"The tests did not finish within {result.timeout_seconds:g} seconds and were killed. "
            "The rule almost certainly loops or waits on something; it should do a bounded amount "
            "of arithmetic over the history it is given and return."
        )

    report = "\n".join(part for part in (result.stdout, result.stderr) if part.strip())
    return f"{result.summary()}\n\n{_excerpt(report, limit) or '<no output>'}"


def write_artifact(
    description: str,
    *,
    rule_source: str,
    test_source: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    now: datetime | None = None,
) -> Path:
    """Write a verified candidate and its tests into a fresh directory. Returns the directory.

    One directory per run, named for the description and stamped with the time, because two runs of
    the same description produce two different candidates and overwriting the first would discard
    something a human may be in the middle of reading.

    The rule file is written *without* the harness prelude — as the model wrote it. That is the
    form a reviewer must judge, since it is the form that gets ported into the canon; the prelude is
    a sandbox loading detail and putting it in the artifact would suggest it belongs in the canon
    too.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    directory = output_dir / f"{_slug(description)}-{stamp}"
    directory.mkdir(parents=True, exist_ok=True)

    header = _artifact_header(description)
    (directory / "rule.py").write_text(header + rule_source.rstrip() + "\n", encoding="utf-8")
    (directory / "test_rule.py").write_text(header + test_source.rstrip() + "\n", encoding="utf-8")
    return directory


def _artifact_header(description: str) -> str:
    return (
        "# GENERATED by rules/generation/loop.py. Reviewed by a human before it is anything else.\n"
        "#\n"
        f"# Constraint: {description.strip()}\n"
        "#\n"
        "# Nothing in rules/generated/ is imported by the engine or the booking API, and this\n"
        "# directory is gitignored. To adopt this rule, read it, port it into rules/rules/ by\n"
        "# hand, and send it through a PR like any other code.\n"
        "#\n"
        "# BaseRule, RuleResult, Context, BookingRequest, BookingRecord and Weekday are free\n"
        "# names here: the rule contract forbids importing them, and the loader binds them. See\n"
        "# rules/generation/harness.py.\n"
        "\n"
    )


_NON_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(description: str) -> str:
    """A short filesystem-safe stem from a rule description."""
    slug = _NON_SLUG.sub("-", description.strip().lower()).strip("-")[:60].strip("-")
    return slug or "rule"


def _excerpt(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated at {limit} characters]"
