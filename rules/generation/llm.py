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

**``GoogleAIStudioClient`` calls a hosted Gemini model over AI Studio's REST API** — the first
implementation here that is both measurable (unlike the CLI) and backed by a frontier model
(unlike anything Ollama has been shown to serve; see ``rule-engine.md``, "No local model tested
holds the contract"). It follows ``OllamaClient``'s three-part split (a pure ``build_*_request``, a
pure ``interpret_*_result``, one socket-touching function) for the identical reason: testable
without a network. The system prompt goes in ``systemInstruction``, never folded into the user
turn, for the same reason ``/api/chat`` is used over ``/api/generate`` above. Its API key travels
in the ``x-goog-api-key`` header and never the URL, because a URL is what every proxy and
exception handler in the path logs and this one is a credential.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .errors import LLMCallError

logger = logging.getLogger(__name__)

__all__ = [
    "LLMResponse",
    "LLMClient",
    "RecordedExchange",
    "RecordingClient",
    "ClaudeCliClient",
    "build_command",
    "interpret_cli_result",
    "OllamaClient",
    "build_chat_request",
    "interpret_ollama_result",
    "GoogleAIStudioClient",
    "build_generate_content_request",
    "interpret_google_result",
    "read_google_api_key",
    "DEFAULT_CLI_MODEL",
    "DEFAULT_OLLAMA_MODEL",
    "DEFAULT_GOOGLE_MODEL",
    "DEFAULT_STUB_MODEL",
    "DEFAULT_CLI_EXECUTABLE",
    "DEFAULT_CLI_TIMEOUT_SECONDS",
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_OLLAMA_TIMEOUT_SECONDS",
    "DEFAULT_GOOGLE_BASE_URL",
    "DEFAULT_GOOGLE_TIMEOUT_SECONDS",
    "GOOGLE_API_KEY_ENV_VAR",
]

#: A model id belongs to the backend that can serve it, so each client carries its own default and
#: there is no package-wide one. There was, and it was wrong in the only way that matters: a single
#: ``DEFAULT_MODEL`` of ``claude-opus-4-8`` reached ``GoogleAIStudioClient`` unchanged whenever a
#: caller named no model, and every such call died on a 404 for an Anthropic model id at Google's
#: endpoint — an error that reads as a credential problem and is not one. A caller that names no
#: model now gets the default of the client it is actually calling.
#:
#: Opus is the CLI's default deliberately: a subtly wrong rule silently mis-enforces real bookings,
#: and every retry costs a full generate-plus-test cycle, so the cheaper model is not obviously
#: cheaper end to end. The benchmark cannot measure this client at all (see the module docstring),
#: so this one remains a judgement rather than a measurement.
DEFAULT_CLI_MODEL = "claude-opus-4-8"

#: A model small enough to be a reasonable first thing to try on a laptop CPU. Not a claim that it
#: holds the contract — that is what running the benchmark against it is for, and no local model
#: tested has held it (``rule-engine.md``).
DEFAULT_OLLAMA_MODEL = "qwen2.5:1.5b"

#: Unlike the ollama default above, this one *is* a claim that the model holds the contract: it
#: took all five golden examples on the first attempt, in ten calls and 26 seconds
#: (``rule-engine.md``). It is also the tier a free key can actually finish a run on — the flagship
#: ``gemini-3.x-flash`` models cap at 20 requests per day per model there, below what a five-example
#: run costs when anything retries.
DEFAULT_GOOGLE_MODEL = "gemini-3.1-flash-lite"

#: What the stub reports having used. It calls nothing, so this names no real backend and exists so
#: that a stub-backed run still records *a* model rather than a null nobody can interpret later.
DEFAULT_STUB_MODEL = "stub"

DEFAULT_CLI_EXECUTABLE = "claude"

#: Wall clock for one CLI call. Generous: the CLI spends over a second on startup before the first
#: token, and a rule with a long system prompt is not a fast completion.
DEFAULT_CLI_TIMEOUT_SECONDS = 180.0

#: Where the Ollama daemon listens by default.
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"

#: Wall clock for one chat call. A 1.5B model on CPU against this package's system prompt is not a
#: fast completion — generous is the safe side to be wrong on.
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 600.0

#: Google's own hosted endpoint. Unlike Ollama there is no daemon to point at a different address,
#: so this is not a per-deployment setting, only a constructor default.
DEFAULT_GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com"

#: Wall clock for one ``generateContent`` call. A Gemini 3 model can spend hundreds of tokens
#: thinking before it answers anything — measured live at 651 ``thoughtsTokenCount`` for a 6-token
#: visible answer — so this is generous for the same reason ``DEFAULT_OLLAMA_TIMEOUT_SECONDS`` is.
DEFAULT_GOOGLE_TIMEOUT_SECONDS = 120.0

#: The environment variable, and the ``rules/.env`` key, this client's credential is read from.
#: Named once so every message below that has to name it — a missing key, an invalid one — says
#: the same thing.
GOOGLE_API_KEY_ENV_VAR = "GOOGLE_STUDIO_API_KEY"

#: ``rules/.env`` itself, resolved from this file's own location rather than the process's current
#: directory. ``benchmark.py`` is documented to be run from ``rules/``, but nothing enforces that,
#: and a bare relative path would silently miss the file from anywhere else.
_DEFAULT_GOOGLE_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


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

    #: The model this backend uses when a caller names none. Part of the protocol rather than each
    #: client's private business because the callers that need it — the retry loop, the benchmark,
    #: the backend's job runner — all hold an ``LLMClient`` and none of them knows which one. The
    #: alternative is a client-name-to-model map maintained somewhere else, which is what this
    #: package had: one copy in ``benchmark.py``, which the backend could not import and therefore
    #: did not have, so the backend sent every client the CLI's model id.
    default_model: str

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        """Return the model's completion for ``prompt`` under ``system``.

        ``None`` means this client's own ``default_model``, resolved by the client. It is never
        passed on to a backend as a literal, and the returned ``LLMResponse.model`` always names
        the model actually used, so a recorder at this seam has something concrete to store.
        """
        ...


@dataclass(frozen=True)
class RecordedExchange:
    """One completion, with the turn that produced it. What ``RecordingClient`` accumulates.

    ``system`` and ``prompt`` are kept alongside ``response`` because a response on its own does
    not say what was asked — and what was asked is the half of a generation run that the retry loop
    itself discards (``run_generation_loop`` returns strings and drops every ``LLMResponse``; see
    that module's docstring for why that stays true). This dataclass is what a caller sitting at
    this seam gets to keep instead: the whole exchange, both directions, one object per call.
    """

    system: str
    prompt: str
    model: str
    response: LLMResponse


@dataclass
class RecordingClient:
    """Wraps an ``LLMClient`` and remembers every exchange it mediated, in both directions.

    This is the whole reason the ``LLMClient`` seam exists in this shape: the loop calls
    ``client.complete`` and nothing else, so anything that also implements ``complete`` can sit
    between the loop and the real backend without the loop knowing the difference. It started life
    inside ``benchmark.py``, recording only the ``LLMResponse`` half — enough for a benchmark, which
    only ever asks "what did this cost" — and is promoted here, recording the ``system``/``prompt``
    half too, because the backend's job runner needs the question as much as the answer: on a
    retry, ``generator.build_prompt`` hands the model back its own failing source and the failure
    text verbatim, and that turn is the entire point of persisting exchanges at all
    (``rule-engine.md``, "Recording what was said to the model").

    Construct one per run — a fresh ``exchanges`` list — wrapping the real client, and read
    ``exchanges`` back once the loop returns. ``responses`` stays as a read-only view for the sake
    of every caller that only ever wanted the old, response-only shape: ``benchmark.py`` and its
    tests build reports from a list of ``LLMResponse``, and re-deriving that list from
    ``exchanges`` here is what lets neither have to change beyond an import.

    ``on_exchange``, when given, is called with each ``RecordedExchange`` immediately after the
    call that produced it returns — which is how the backend's job runner persists one exchange row
    per model call as a multi-minute generation run progresses, rather than only once at the end
    when there would be nothing left to resume from if the process died mid-run. A hook that raises
    is caught and logged rather than left to propagate: the hook is a side effect of recording,
    never a step the generation loop itself depends on, and letting a database write failure abort
    an in-flight model call would fail the *booking* feature (rule generation) for a *reporting*
    reason (an exchange could not be persisted) — the opposite of this project's fail-closed
    posture, which reserves failing hard for cases where the outcome itself cannot be trusted.
    """

    wrapped: LLMClient
    on_exchange: Callable[[RecordedExchange], None] | None = None
    exchanges: list[RecordedExchange] = field(default_factory=list)

    @property
    def default_model(self) -> str:
        """The wrapped client's, never one of this wrapper's own.

        Recording a call must not change which model it goes to, and a default declared here would
        do exactly that for every caller that names none — silently, since both values are strings
        and nothing downstream could tell they had been swapped.
        """
        return self.wrapped.default_model

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        response = self.wrapped.complete(system=system, prompt=prompt, model=model)
        # `response.model`, not the `model` argument: that argument may be None, meaning "whatever
        # the wrapped client defaults to", and a recorded exchange saying the model was None is
        # exactly the row someone reads a year later when asking which model wrote a live rule.
        exchange = RecordedExchange(
            system=system, prompt=prompt, model=response.model, response=response
        )
        self.exchanges.append(exchange)
        if self.on_exchange is not None:
            try:
                self.on_exchange(exchange)
            except Exception:
                logger.exception(
                    "on_exchange hook raised while recording a completion; the exchange is kept "
                    "in memory (see `exchanges`) but may not have been persisted."
                )
        return response

    @property
    def responses(self) -> list[LLMResponse]:
        """Every response seen, in order — the shape this class had before promotion."""
        return [exchange.response for exchange in self.exchanges]


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

    default_model = DEFAULT_CLI_MODEL

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        model = model or self.default_model
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

    default_model = DEFAULT_OLLAMA_MODEL

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        model = model or self.default_model
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
    except OSError as exc:
        # A connection dropped *after* the request was sent surfaces from `http.client` while
        # reading the response — a bare `ConnectionResetError`, which `urlopen` does not wrap in a
        # `URLError` and the branches above therefore all miss. Left uncaught it escapes this
        # module entirely, which is the one thing this client promises never to do: a transport
        # failure that is not an `LLMCallError` is not recorded as a `CALL_ERROR` by the caller,
        # so a checkpointed benchmark run dies with a traceback and no row for the example it was
        # on. `OSError` is deliberately the widest net and deliberately last, since `URLError`,
        # `HTTPError` and `TimeoutError` are all `OSError` subclasses handled above on their own
        # terms.
        raise LLMCallError(
            f"Ollama's connection to {base_url!r} failed mid-request: {exc}"
        ) from exc


class GoogleAIStudioClient:
    """Calls a Gemini model through Google AI Studio's REST API, over ``urllib.request``.

    The first ``LLMClient`` here that is both measurable and backed by a frontier model:
    ``ClaudeCliClient`` cannot serve a benchmark at all (module docstring), and no model
    ``OllamaClient`` was benchmarked against holds the rule contract (``rule-engine.md``). This one
    is neither confined to a laptop CPU's local model selection nor opaque to token accounting.

    The system prompt goes in ``systemInstruction`` and is never folded into the user turn — the
    same reasoning ``OllamaClient`` gives for ``/api/chat`` over ``/api/generate``: this package's
    system prompt is long, constraint-dense, and exactly the thing under test.

    The API key travels in the ``x-goog-api-key`` header and never in the URL or a query string.
    A URL is what every proxy and exception handler in the path logs, and this one is a credential
    — keeping it out of the URL is what lets every error message below safely include the URL.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_GOOGLE_BASE_URL,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_GOOGLE_TIMEOUT_SECONDS,
        temperature: float | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds!r}")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature

    default_model = DEFAULT_GOOGLE_MODEL

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        model = model or self.default_model
        if not self.api_key:
            raise LLMCallError(
                f"No Google AI Studio API key configured. Set {GOOGLE_API_KEY_ENV_VAR} in the "
                "environment, or as a KEY=value line in rules/.env (gitignored)."
            )
        url, body = build_generate_content_request(
            self.base_url,
            system=system,
            prompt=prompt,
            model=model,
            temperature=self.temperature,
        )
        status, response_body = _send_generate_content_request(
            url,
            body,
            api_key=self.api_key,
            timeout_seconds=self.timeout_seconds,
            base_url=self.base_url,
        )
        return interpret_google_result(
            status=status, body=response_body, model=model, base_url=self.base_url
        )


def build_generate_content_request(
    base_url: str,
    *,
    system: str,
    prompt: str,
    model: str,
    temperature: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """The exact URL and decoded request body for one ``generateContent`` call.

    Split out from ``complete`` for the same reason ``build_chat_request`` is: the one part of
    this client whose correctness is a matter of shape, and a test that had to hit the live
    endpoint to check it would be a test that spends money.

    ``seed`` is deliberately never sent, in this function or anywhere else in this client.
    ``generationConfig`` rejects an unknown key with a 400 ("Unknown name ... Cannot find field"),
    so acceptance of a ``seed`` field would be meaningful — but two calls made against the live API
    with an identical ``seed`` and ``temperature: 1.0`` returned different completions, and a third
    call with a different seed returned a third completion. The field is accepted and not honoured.
    Sending it anyway would leave ``benchmark.py`` no honest answer for whether to record a value
    it cannot confirm was applied; not sending it is what lets the benchmark record ``seed`` as
    unset for this client rather than claim a value it did not apply. Do not re-add it on the
    strength of the field existing in the schema — this was checked against the live API, not
    assumed from the docs.
    """
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    if temperature is not None:
        body["generationConfig"] = {"temperature": temperature}
    return f"{base_url}/v1beta/models/{model}:generateContent", body


def interpret_google_result(*, status: int, body: str, model: str, base_url: str) -> LLMResponse:
    """Turn one finished ``generateContent`` response into an ``LLMResponse``, or raise
    ``LLMCallError``.

    Keyed on HTTP status first, the same discipline ``interpret_ollama_result`` follows — with one
    wrinkle Google's API has and Ollama's does not: **an invalid key comes back as HTTP 400**
    (``"API key not valid. Please pass a valid API key."``, ``status: INVALID_ARGUMENT``, a
    ``details[].reason`` of ``API_KEY_INVALID``), not 401, confirmed against the live API rather
    than assumed. A 400 of that specific shape is reported as a credential failure; any other 400
    stays a generic failure, because blaming the key for an unrelated malformed request would send
    a developer down the wrong path.

    A blocked prompt, an empty candidate list, and a candidate that finished without producing text
    (``SAFETY``, ``RECITATION``, ``MAX_TOKENS``, or genuinely empty) are all raised from here too,
    never returned as an ``LLMResponse`` with empty ``text`` — the same lesson
    ``interpret_cli_result`` documents: a transport or backend failure passed on as a completion
    gets blamed on the model, several layers later, as a syntax error.
    """
    if status in (401, 403):
        raise LLMCallError(
            f"Google AI Studio rejected the API key (HTTP {status}). {GOOGLE_API_KEY_ENV_VAR} is "
            "missing, wrong, or not enabled for the Generative Language API. "
            f"Server said: {_excerpt(body) or '<no detail>'}",
            exit_code=status,
            stderr=body,
        )
    if status == 400 and _is_api_key_invalid(body):
        raise LLMCallError(
            "Google AI Studio rejected the API key (HTTP 400, API_KEY_INVALID). "
            f"{GOOGLE_API_KEY_ENV_VAR} is missing, wrong, or not enabled for the Generative "
            f"Language API. Server said: {_excerpt(body) or '<no detail>'}",
            exit_code=400,
            stderr=body,
        )
    if status == 404:
        raise LLMCallError(
            f"Google AI Studio has no model {model!r} (404 from {base_url}). "
            f"GET {base_url}/v1beta/models lists what this key can reach. "
            f"Server said: {_excerpt(body) or '<no detail>'}",
            exit_code=404,
            stderr=body,
        )
    if status == 429:
        raise LLMCallError(
            "Google AI Studio rate-limited this call (HTTP 429). AI Studio's free tier enforces "
            "real per-minute request limits, so a benchmark run that dies here partway through is "
            "one rate limit, not five separate model failures. This client does not retry: the "
            "generation loop deliberately never retries an LLMCallError, and a silent retry here "
            f"would hide the limit instead of reporting it. Server said: "
            f"{_excerpt(body) or '<no detail>'}",
            exit_code=429,
            stderr=body,
        )
    if status != 200:
        raise LLMCallError(
            f"Google AI Studio returned HTTP {status} from {base_url}: "
            f"{_excerpt(body) or '<no detail>'}",
            exit_code=status,
            stderr=body,
        )

    payload = _parse_google_payload(body, status=status)

    block_reason = _get(payload, "promptFeedback", "blockReason")
    if block_reason:
        raise LLMCallError(
            f"Google AI Studio blocked this prompt (blockReason={block_reason!r}, "
            f"status {status}): {_excerpt(body)}",
            exit_code=status,
            stderr=body,
        )

    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise LLMCallError(
            f"Google AI Studio returned no candidates (status {status}): {_excerpt(body)}",
            exit_code=status,
            stderr=body,
        )

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason") if isinstance(candidate, Mapping) else None
    text = _join_google_text_parts(candidate) if isinstance(candidate, Mapping) else ""

    if not text and finish_reason == "MAX_TOKENS":
        raise LLMCallError(
            "Google AI Studio hit MAX_TOKENS with no text produced. A generated rule is a few "
            "dozen lines, so hitting the output token limit is a configuration fact worth naming "
            f"rather than an empty completion left to be rejected later as bad rule source. "
            f"{_excerpt(body)}",
            exit_code=status,
            stderr=body,
        )
    if not text and finish_reason not in (None, "STOP"):
        raise LLMCallError(
            f"Google AI Studio's candidate finished with reason {finish_reason!r} and no text "
            f"(status {status}): {_excerpt(body)}",
            exit_code=status,
            stderr=body,
        )
    if not text:
        raise LLMCallError(
            f"Google AI Studio returned an empty completion (status {status}): {_excerpt(body)}",
            exit_code=status,
            stderr=body,
        )

    usage = payload.get("usageMetadata")
    usage = usage if isinstance(usage, Mapping) else {}
    candidates_tokens = _as_int(usage.get("candidatesTokenCount"))
    thoughts_tokens = _as_int(usage.get("thoughtsTokenCount"))
    # Deliberately not the ticket's simpler candidatesTokenCount -> output_tokens: on a thinking
    # model that field counts only the visible answer. A measured gemini-3.5-flash call reported 6
    # against a thoughtsTokenCount of 651 — the simple mapping would understate what the prompt
    # actually cost by two orders of magnitude, and this number exists to be compared across
    # models. The split stays recoverable in `raw` either way.
    if thoughts_tokens is not None:
        output_tokens = (candidates_tokens or 0) + thoughts_tokens
    else:
        output_tokens = candidates_tokens

    return LLMResponse(
        text=text,
        model=model,
        input_tokens=_as_int(usage.get("promptTokenCount")),
        output_tokens=output_tokens,
        # A hardcoded price table goes stale silently and is then reported as fact — the same
        # argument that keeps OllamaClient's cost None.
        cost_usd=None,
        # The API does not report call duration; inventing one here would be a number the caller
        # cannot tell apart from a measured one.
        duration_ms=None,
        raw=payload,
    )


def _is_api_key_invalid(body: str) -> bool:
    """True only for Google's specific 400 shape for a bad key, confirmed against a live call:

    ``{"error": {"status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID", ...}],
    "message": "API key not valid. Please pass a valid API key."}}``.

    Checks ``details[].reason`` first and falls back to the message text, since ``details`` is
    not documented as guaranteed present on every response shape this endpoint might return.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return False
    for detail in error.get("details") or []:
        if isinstance(detail, Mapping) and detail.get("reason") == "API_KEY_INVALID":
            return True
    message = error.get("message")
    return isinstance(message, str) and "API key not valid" in message


def _get(payload: Mapping[str, Any], *keys: str) -> Any:
    """Walk nested ``Mapping`` lookups, returning ``None`` the moment one is missing or not a
    ``Mapping`` rather than raising — Google's error/feedback shapes are not guaranteed present."""
    value: Any = payload
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _join_google_text_parts(candidate: Mapping[str, Any]) -> str:
    """Join the ``text`` of every part that is not a thought part.

    ``finishReason: MAX_TOKENS`` comes back with ``content`` as an empty object — no ``parts`` key
    at all, confirmed against the live API — so "no parts to join" is a real, reachable state
    handled here rather than defended against speculatively. A Gemini 3 model's thinking parts
    carry ``thought: true`` (and/or a ``thoughtSignature``) and no usable answer text; skipping
    them is what keeps a thinking model's completion from being polluted by its own scratch work.
    """
    content = candidate.get("content")
    if not isinstance(content, Mapping):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if not isinstance(part, Mapping) or part.get("thought"):
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
    return "".join(texts)


def _parse_google_payload(body: str, *, status: int) -> Mapping[str, Any]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMCallError(
            f"Google AI Studio did not return JSON (status {status}): "
            f"{_excerpt(body) or '<empty>'}",
            exit_code=status,
            stderr=body,
        ) from exc
    if not isinstance(payload, Mapping):
        raise LLMCallError(
            f"Google AI Studio returned JSON that is not an object (status {status}): "
            f"{_excerpt(body)}",
            exit_code=status,
            stderr=body,
        )
    return payload


def _send_generate_content_request(
    url: str,
    body: Mapping[str, Any],
    *,
    api_key: str,
    timeout_seconds: float,
    base_url: str,
) -> tuple[int, str]:
    """The one function in this client that touches a socket. Everything else is pure.

    The key is set as the ``x-goog-api-key`` header and appears nowhere else — not in the URL, not
    in a query string — which is what lets every message here and in ``interpret_google_result``
    safely include the URL: it never carried a credential in the first place, so there is nothing
    for a proxy log or an exception handler to leak.

    Returns ``(status, response_text)`` for anything Google actually answered, including an error
    — ``interpret_google_result`` is what turns a 400/404/429 body into the right message. Only a
    request the server never got to answer at all — refused, or too slow — raises from here
    directly, because there is no status/body pair to hand back for either.
    """
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")
    except TimeoutError as exc:
        raise LLMCallError(
            f"Google AI Studio did not answer within {timeout_seconds:g}s ({base_url})."
        ) from exc
    except urllib.error.URLError as exc:
        # A connect timeout surfaces as a URLError wrapping a TimeoutError, not as a bare
        # TimeoutError — the same non-obvious behaviour _send_chat_request documents, confirmed
        # against a real socket rather than assumed — so the timeout message above would never
        # fire without unwrapping `.reason` here too.
        if isinstance(exc.reason, TimeoutError):
            raise LLMCallError(
                f"Google AI Studio did not answer within {timeout_seconds:g}s ({base_url})."
            ) from exc
        raise LLMCallError(f"Google AI Studio is not answering at {base_url!r}.") from exc
    except OSError as exc:
        # Same net, same reasoning, as `_send_chat_request`'s: a connection reset by the far end
        # while the response is being read is a bare `ConnectionResetError` that `urlopen` does not
        # wrap, so none of the branches above see it. This one is not hypothetical — a hosted
        # endpoint resets a long generateContent call often enough that a five-example benchmark
        # run met it, and an uncaught one aborts that run with a traceback instead of a recorded
        # `CALL_ERROR` the checkpoint can resume past. `OSError` last, for the subclass ordering
        # reason given there.
        raise LLMCallError(
            f"Google AI Studio's connection to {base_url} failed mid-request: {exc}"
        ) from exc


def read_google_api_key(env_path: Path | str = _DEFAULT_GOOGLE_ENV_PATH) -> str | None:
    """The API key: environment first, then ``rules/.env``, ``None`` if neither has it.

    No ``python-dotenv``. ``rules`` and ``generation`` declare zero runtime dependencies
    deliberately — the backend installs this package editable, so a dependency added here is a
    cost the booking API pays forever for what is otherwise a ten-line parser. Blank lines and
    ``#`` comments are skipped, and one matching pair of quotes around the value is stripped, the
    way a shell would strip them.

    Precedence is environment over file so a CI secret or a developer's own shell export always
    wins over a stale value left sitting in the file.
    """
    value = os.environ.get(GOOGLE_API_KEY_ENV_VAR)
    if value:
        return value

    path = Path(env_path)
    if not path.exists():
        return None

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        if key.strip() != GOOGLE_API_KEY_ENV_VAR:
            continue
        found = raw_value.strip()
        if len(found) >= 2 and found[0] == found[-1] and found[0] in "\"'":
            found = found[1:-1]
        return found or None

    return None
