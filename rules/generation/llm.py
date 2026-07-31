"""The seam between the generation agents and whatever actually calls a model.

One method, ``complete(system, prompt, model) -> LLMResponse``. The agents in this package are
written against that and nothing else, so the backend is a constructor argument rather than a
rewrite.

**``ClaudeCliClient``** shells out to ``claude -p``. It needs no API key, only a Claude Code CLI
that is installed and interactively authenticated, which is why the generation loop is buildable
now. That is an acceptable dependency for a *developer* tool whose output is a file a human reviews
before it is committed; it would not be acceptable for anything the booking API calls at request
time, and nothing here is.

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

**``OllamaClient`` calls a local model through a running Ollama daemon**, which is what makes the
benchmark this package exists to feed possible without cloud spend or an API key: it hits
``POST /api/chat`` rather than ``/api/generate`` because chat carries the system prompt as its own
message, and this package's system prompt is long and constraint-dense — the very thing under test.
Folding it into the user turn for ``/api/generate`` would benchmark a prompt nobody ships.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .errors import LLMCallError

__all__ = [
    "LLMResponse",
    "LLMClient",
    "ClaudeCliClient",
    "build_command",
    "interpret_cli_result",
    "OllamaClient",
    "build_chat_request",
    "interpret_ollama_result",
    "DEFAULT_MODEL",
    "DEFAULT_CLI_EXECUTABLE",
    "DEFAULT_CLI_TIMEOUT_SECONDS",
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_OLLAMA_TIMEOUT_SECONDS",
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

#: Where the Ollama daemon listens by default.
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"

#: Wall clock for one chat call. A 1.5B model on CPU against this package's system prompt is not a
#: fast completion — generous is the safe side to be wrong on.
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 600.0


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


class OllamaClient:
    """Calls a model served by a local Ollama daemon, over its HTTP API.

    Talks ``/api/chat``, never ``/api/generate`` — see the module docstring for why the split
    matters here specifically: this package's system prompt is the thing under test, and folding it
    into the user turn would benchmark a prompt nobody ships.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds!r}")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        # Copied rather than held by reference, and only when non-empty: 6.2 passes a fixed `seed`
        # and `temperature` so two benchmark runs are comparable, and an absent key here is what
        # keeps a call with nothing to say about `options` byte-identical to one made before this
        # argument existed.
        self.options = dict(options) if options else None

    def complete(self, *, system: str, prompt: str, model: str = DEFAULT_MODEL) -> LLMResponse:
        url, body = build_chat_request(
            self.base_url, system=system, prompt=prompt, model=model, options=self.options
        )
        status, response_body = _send_chat_request(
            url, body, timeout_seconds=self.timeout_seconds, base_url=self.base_url
        )
        return interpret_ollama_result(
            status=status, body=response_body, model=model, base_url=self.base_url
        )


def build_chat_request(
    base_url: str,
    *,
    system: str,
    prompt: str,
    model: str,
    options: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """The exact URL and decoded request body for one ``/api/chat`` call.

    Split out from ``complete`` for the same reason ``build_command`` is: it is the one part of
    this client whose correctness is a matter of shape, and a test that had to run a daemon to
    check it would be a test that calls the model.

    ``stream`` is always ``False``. Ollama's default is newline-delimited JSON, one object per
    token; parsing only the first yields an empty completion, which would flow into the fence
    stripper and be rejected several layers later as bad rule source — blaming the model for a
    transport choice made here.
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    if options:
        body["options"] = dict(options)
    return f"{base_url}/api/chat", body


def interpret_ollama_result(*, status: int, body: str, model: str, base_url: str) -> LLMResponse:
    """Turn one finished ``/api/chat`` response into an ``LLMResponse``, or raise ``LLMCallError``.

    Keyed on the HTTP status before anything about the body is trusted. A model that is not pulled
    is Ollama's own 404, naming the model in its own error text; every other non-200 is surfaced
    generically rather than guessed at.
    """
    if status == 404:
        raise LLMCallError(
            f"Ollama does not have {model!r} pulled (404 from {base_url}). "
            f"Run `ollama pull {model}` and retry. Daemon said: {_excerpt(body) or '<no detail>'}",
            exit_code=status,
            stderr=body,
        )
    if status != 200:
        raise LLMCallError(
            f"Ollama returned HTTP {status} from {base_url}: {_excerpt(body) or '<no detail>'}",
            exit_code=status,
            stderr=body,
        )

    payload = _parse_ollama_payload(body, status=status)

    message = payload.get("message")
    if not isinstance(message, Mapping):
        raise LLMCallError(
            f"Ollama's response has no 'message' object to read a completion from "
            f"(status {status}): {_excerpt(body)}",
            exit_code=status,
            stderr=body,
        )

    text = _as_text(message.get("content"))
    if not text:
        raise LLMCallError(
            f"Ollama returned no completion text in 'message.content' (status {status}): "
            f"{_excerpt(body)}",
            exit_code=status,
            stderr=body,
        )

    # Ollama reports total_duration in nanoseconds; LLMResponse.duration_ms is milliseconds.
    duration_ns = _as_int(payload.get("total_duration"))
    return LLMResponse(
        text=text,
        model=model,
        input_tokens=_as_int(payload.get("prompt_eval_count")),
        output_tokens=_as_int(payload.get("eval_count")),
        # A local model's price is not zero dollars in the sense 0.0 would claim; it is a number
        # this backend has no way to know, so it stays unset like every other metadata field a
        # backend cannot report.
        cost_usd=None,
        duration_ms=duration_ns // 1_000_000 if duration_ns is not None else None,
        raw=payload,
    )


def _parse_ollama_payload(body: str, *, status: int) -> Mapping[str, Any]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMCallError(
            f"Ollama did not return JSON (status {status}): {_excerpt(body) or '<empty>'}",
            exit_code=status,
            stderr=body,
        ) from exc
    if not isinstance(payload, Mapping):
        raise LLMCallError(
            f"Ollama returned JSON that is not an object (status {status}): {_excerpt(body)}",
            exit_code=status,
            stderr=body,
        )
    return payload


def _send_chat_request(
    url: str, body: Mapping[str, Any], *, timeout_seconds: float, base_url: str
) -> tuple[int, str]:
    """The one function that touches a socket. Everything else in this client is pure.

    Returns ``(status, response_text)`` for any request the daemon actually answered — a 404 is
    still an answer, and ``interpret_ollama_result`` is what turns that into the "run ollama pull"
    message. Only a request the daemon never got to answer at all — refused, or too slow — raises
    from here directly, because there is no status/body pair to hand back for either.
    """
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")
    except TimeoutError as exc:
        raise LLMCallError(
            f"Ollama did not answer within {timeout_seconds:g}s ({base_url})."
        ) from exc
    except urllib.error.URLError as exc:
        # A connect timeout surfaces as a URLError wrapping a TimeoutError, not as a bare
        # TimeoutError — confirmed against a real socket, not assumed from the docs — so the
        # timeout message above would never fire without unwrapping `.reason` here too.
        if isinstance(exc.reason, TimeoutError):
            raise LLMCallError(
                f"Ollama did not answer within {timeout_seconds:g}s ({base_url})."
            ) from exc
        raise LLMCallError(
            f"Ollama's daemon is not answering at {base_url!r}. Is `ollama serve` running?"
        ) from exc
