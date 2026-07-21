"""The seam between the generation agents and whatever actually calls a model.

One method, ``complete(system, prompt, model) -> LLMResponse``. The agents in this package are
written against that and nothing else, so the backend is a constructor argument rather than a
rewrite.

**One implementation ships today: ``ClaudeCliClient``**, which shells out to ``claude -p``. It needs
no API key, only a Claude Code CLI that is installed and interactively authenticated, which is why
the generation loop is buildable now. That is an acceptable dependency for a *developer* tool whose
output is a file a human reviews before it is committed; it would not be acceptable for anything the
booking API calls at request time, and nothing here is.

**Why an SDK client is a separate implementation and not a flag on this one.** The benchmark exists
to log token usage, latency and cost per prompt, and the CLI cannot report those for the prompt it
was given. Measured on a development machine, a call whose real prompt is 10 input / 40 output
tokens is billed for ~11.5k tokens of Claude Code harness preamble — carried in
``cache_read_input_tokens`` and ``cache_creation_input_tokens`` — and costs $0.015–0.023, with a
second hidden model call reported in ``modelUsage`` and ~1.5s of startup before the first token.
``--system-prompt`` together with ``--exclude-dynamic-system-prompt-sections`` does *not* strip it:
overhead stayed at 11,458 tokens and the cost *rose*, by losing the cache hit. A benchmark run
through this client would faithfully measure Claude Code and say nothing about the prompt under
test. An SDK-backed ``LLMClient`` plugs in exactly here — same protocol, same ``LLMResponse`` — and
is what the benchmark must be given.

``LLMResponse`` metadata is therefore optional throughout: a backend reports what it can, and a
consumer that needs a number the backend does not have is asking the wrong backend rather than
reading a fabricated zero.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .errors import LLMCallError

__all__ = [
    "LLMResponse",
    "LLMClient",
    "ClaudeCliClient",
    "build_command",
    "interpret_cli_result",
    "DEFAULT_MODEL",
    "DEFAULT_CLI_EXECUTABLE",
    "DEFAULT_CLI_TIMEOUT_SECONDS",
]

#: The model every agent in this package uses unless told otherwise. Opus is the default
#: deliberately: a subtly wrong rule silently mis-enforces real bookings, and every retry costs a
#: full generate-plus-test cycle, so the cheaper model is not obviously cheaper end to end. The
#: benchmark settles that with numbers; until it runs, this is the safe side to be wrong on.
DEFAULT_MODEL = "claude-opus-4-8"

DEFAULT_CLI_EXECUTABLE = "claude"

#: Wall clock for one CLI call. Generous: the CLI spends over a second on startup before the first
#: token, and a rule with a long system prompt is not a fast completion.
DEFAULT_CLI_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class LLMResponse:
    """One completion, plus whatever the backend was able to say about what it cost.

    Every metadata field is optional. A backend that does not report token counts leaves them
    ``None``, which a consumer can see and act on; filling them with zeros would be
    indistinguishable from a free call.

    ``raw`` keeps the backend's own payload so a caller can reach a field this dataclass does not
    model — the CLI's ``modelUsage`` breakdown, for instance — without this type growing a column
    per backend.
    """

    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    """What the generation agents require of a model backend: one call, one response.

    Deliberately minimal. Anything richer — streaming, tool use, multi-turn — is a capability no
    agent here uses, and a protocol method nobody calls is one every future implementation still has
    to provide.

    An implementation raises ``LLMCallError`` when it cannot produce a completion. It never returns
    an ``LLMResponse`` describing a failure: an empty ``text`` would flow into the fence stripper
    and be rejected several layers later as bad rule source, blaming the model for the backend.
    """

    def complete(self, *, system: str, prompt: str, model: str = DEFAULT_MODEL) -> LLMResponse:
        """Return the model's completion for ``prompt`` under ``system``."""
        ...


class ClaudeCliClient:
    """Calls the model by shelling out to the Claude Code CLI in print mode.

    ``--max-turns 1`` and an empty ``--allowedTools`` are what keep this a completion rather than an
    agent session: no tool is available to it, so it cannot read the repository it happens to be
    invoked from, and it gets exactly one turn in which to answer.
    """

    def __init__(
        self,
        *,
        executable: str = DEFAULT_CLI_EXECUTABLE,
        timeout_seconds: float = DEFAULT_CLI_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds!r}")
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def complete(self, *, system: str, prompt: str, model: str = DEFAULT_MODEL) -> LLMResponse:
        command = build_command(prompt, model=model, system=system, executable=self.executable)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise LLMCallError(
                f"The Claude CLI ({self.executable!r}) is not on PATH. "
                "This client drives an installed, interactively authenticated Claude Code."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMCallError(
                f"The Claude CLI did not answer within {self.timeout_seconds:g}s."
            ) from exc

        return interpret_cli_result(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            model=model,
        )


def build_command(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
    executable: str = DEFAULT_CLI_EXECUTABLE,
) -> list[str]:
    """The exact argv for one non-interactive CLI completion.

    Split out from ``complete`` so it can be asserted without running anything: this is the one part
    of the client whose correctness is a matter of flags, and a test that had to spawn the binary to
    check them would be a test that calls the model.
    """
    command = [
        executable,
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        # Variadic, and given nothing: the session has no tools at all.
        "--allowedTools",
        "",
        "--max-turns",
        "1",
    ]
    if system is not None:
        command += ["--system-prompt", system]
    return command


def interpret_cli_result(
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    model: str,
) -> LLMResponse:
    """Turn one finished CLI invocation into an ``LLMResponse``, or raise ``LLMCallError``.

    **Failure is keyed on ``is_error``, never on ``subtype``.** A CLI run that 404s on an unknown
    model id exits 1 and reports ``is_error: true`` while still reporting ``subtype: "success"`` —
    reading the subtype would hand the caller an error string as if it were generated rule source,
    which would then be rejected for a syntax error and blamed on the model.

    The payload is parsed before the exit code is consulted, because a failing run still writes its
    JSON to stdout and ``result`` holds the only human-readable account of what went wrong.
    """
    payload = _parse_payload(stdout, exit_code=exit_code, stderr=stderr)

    if payload.get("is_error") or exit_code != 0:
        raise LLMCallError(
            "The Claude CLI reported a failed call"
            f" (exit {exit_code}, subtype {payload.get('subtype')!r},"
            f" api_error_status {payload.get('api_error_status')!r}):"
            f" {_as_text(payload.get('result')) or '<no detail>'}",
            exit_code=exit_code,
            stderr=stderr,
        )

    text = _as_text(payload.get("result"))
    if text is None:
        raise LLMCallError(
            "The Claude CLI returned a successful result with no 'result' text.",
            exit_code=exit_code,
            stderr=stderr,
        )

    usage = payload.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    return LLMResponse(
        text=text,
        model=model,
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cost_usd=_as_float(payload.get("total_cost_usd")),
        duration_ms=_as_int(payload.get("duration_ms")),
        raw=payload,
    )


def _parse_payload(stdout: str, *, exit_code: int, stderr: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMCallError(
            f"The Claude CLI did not emit JSON on stdout (exit {exit_code}): "
            f"{_excerpt(stdout) or '<empty>'}",
            exit_code=exit_code,
            stderr=stderr,
        ) from exc
    if not isinstance(payload, Mapping):
        raise LLMCallError(
            f"The Claude CLI emitted JSON that is not an object (exit {exit_code}): "
            f"{_excerpt(stdout)}",
            exit_code=exit_code,
            stderr=stderr,
        )
    return payload


def _as_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _excerpt(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"
