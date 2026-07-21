"""The subprocess sandbox: run candidate rule source, and its tests, out of process.

This is the *runtime* half of safe execution. ``safety.validate_source`` refuses unacceptable source
before anything writes or runs it; this module bounds what running it can cost. Neither is
sufficient alone — the static pass cannot cap CPU or memory, and the sandbox cannot tell a rule that
reads the filesystem from one that reads a booking.

Four bounds are applied to every child process:

* **A wall-clock timeout.** The child is started in its own process session and, on expiry, the
  whole session is killed — not just the direct child. A candidate that spawned something would
  otherwise outlive the run that was supposed to bound it.
* **A memory cap** (``RLIMIT_AS``), plus ``RLIMIT_CORE`` of zero so a crash cannot write a core file
  the size of the address space it was just denied.
* **No inherited environment.** The child gets the small curated map in ``_child_env`` and nothing
  else: no API keys, no ``PYTHONPATH``, no credentials that happen to be exported in the shell that
  started the generation loop. The interpreter runs under ``-E -s -B``, so ``PYTHON*`` variables are
  ignored, the user site directory is off the path, and nothing is written back as bytecode. Full
  ``-I`` is deliberately not used: it also drops the script's own directory from ``sys.path``, which
  is where the candidate module the tests import lives.
* **A fresh temp directory as cwd**, deleted when the run returns. The candidate's own module and
  its test module are the only files written there. Anything the candidate creates goes with it.

**Fail closed.** A timeout and a crash are *not* successes. ``SandboxResult.passed`` is true for
exactly one outcome, ``PASSED``, so a caller cannot mistake "we never found out" for "it works" by
checking the wrong thing. An unverifiable candidate does not advance to the canon. ``run_tests``
treats pytest's "no tests were collected" as a crash for the same reason: a test file that collected
nothing has established nothing.

A misbehaving candidate never raises. Exceptions from this module signal a bug in the *caller* —
an unusable filename, a non-positive timeout — mirroring the controller's split between a denial
(user-facing) and ``ContextMismatchError`` (loud, because someone built the call wrong).

**Platform.** Linux is the target and the reference behaviour. The memory cap is imposed with
``resource.setrlimit(RLIMIT_AS, ...)`` in a ``preexec_fn``, which Linux honours for the child's
whole address space. macOS accepts the same call but does not reliably enforce it — a child there
may allocate past the cap and run to completion. Rather than substitute a weaker mechanism that
behaves the same everywhere, this module implements the Linux behaviour, degrades to *no* memory cap
where ``resource`` or ``RLIMIT_AS`` is absent (``MEMORY_CAP_ENFORCED`` says which), and leaves the
timeout — which is enforced identically on every platform — as the bound that always holds. Tests
that can only pass where the cap is real are skipped elsewhere by name.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

try:  # pragma: no cover - the except branch is unreachable on the platforms CI runs
    import resource
except ImportError:  # pragma: no cover - Windows has no resource module
    resource = None  # type: ignore[assignment]

__all__ = [
    "SandboxOutcome",
    "SandboxResult",
    "run_module",
    "run_tests",
    "run_files",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_MEMORY_LIMIT_BYTES",
    "MEMORY_CAP_ENFORCED",
    "RULE_MODULE_NAME",
    "TEST_MODULE_NAME",
]

#: Wall clock a candidate gets before the session is killed. Generous for date arithmetic over a
#: capped history window, and short enough that a stuck generation loop is noticed in a run rather
#: than in a bill.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Address-space cap. This bounds *virtual* memory, which includes what the interpreter reserves
#: before any candidate code runs, so it is set well above what a rule needs — the cap exists to
#: stop a runaway allocation, not to measure a rule's working set.
DEFAULT_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024

#: Whether ``memory_limit_bytes`` is actually imposed here. False means the timeout is the only
#: bound in force; see the platform note in the module docstring.
MEMORY_CAP_ENFORCED = resource is not None and hasattr(resource, "RLIMIT_AS")

#: Filenames written into the sandbox directory. Fixed rather than caller-supplied so the test
#: module the Tester writes can ``import candidate_rule`` without being told what to call it.
RULE_MODULE_NAME = "candidate_rule.py"
TEST_MODULE_NAME = "test_candidate_rule.py"

#: pytest exits 5 when it collected nothing. That is not a pass — nothing was verified — and it is
#: the likely shape of a Tester that emitted a file with no test functions in it.
_PYTEST_NO_TESTS_COLLECTED = 5

#: -E ignores ``PYTHON*`` variables, -s drops the user site directory, -B writes no bytecode into a
#: directory that is about to be deleted. See the module docstring for why this is not plain ``-I``.
_INTERPRETER_FLAGS = ("-E", "-s", "-B")

#: pytest exits 1 when tests ran and some failed. Every other non-zero code means pytest itself
#: could not do its job (usage error, internal error, interrupt), which is a crash, not a verdict.
_PYTEST_TESTS_FAILED = 1


class SandboxOutcome(str, Enum):
    """What became of one sandboxed run.

    Only ``PASSED`` means the candidate was positively established to work. The other three are
    distinguished because the generation loop reacts differently to each — a failure is fed back to
    the model as a diff to fix, a timeout usually means the rule loops, a crash means it does not
    run at all — but none of them is a success.
    """

    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CRASHED = "crashed"


@dataclass(frozen=True)
class SandboxResult:
    """The structured outcome of a sandboxed run. Never raised, always returned."""

    outcome: SandboxOutcome
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timeout_seconds: float

    @property
    def passed(self) -> bool:
        """True for ``PASSED`` alone. The one question a caller should be asking."""
        return self.outcome is SandboxOutcome.PASSED

    def summary(self) -> str:
        """One-line description for logs. Never shown to a booking user."""
        if self.outcome is SandboxOutcome.TIMEOUT:
            return f"timed out after {self.timeout_seconds:g}s"
        return f"{self.outcome.value} (exit {self.exit_code}) in {self.duration_seconds:.2f}s"


def run_module(
    source: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
    extra_files: Mapping[str, str] | None = None,
) -> SandboxResult:
    """Execute ``source`` as a script in the sandbox.

    There is no test-failure outcome here: a module either executes cleanly or it does not, so any
    non-zero exit is ``CRASHED``. Use it to check that a candidate imports and defines what it
    claims to; use ``run_tests`` to find out whether it is *correct*.
    """
    files = {RULE_MODULE_NAME: source}
    files.update(extra_files or {})
    result = run_files(
        files,
        [RULE_MODULE_NAME],
        timeout_seconds=timeout_seconds,
        memory_limit_bytes=memory_limit_bytes,
    )
    if result.outcome is SandboxOutcome.FAILED:  # pragma: no cover - run_files never returns it
        return _replace_outcome(result, SandboxOutcome.CRASHED)
    return result


def run_tests(
    rule_source: str,
    test_source: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
    extra_files: Mapping[str, str] | None = None,
) -> SandboxResult:
    """Run ``test_source`` (pytest) against ``rule_source`` in the sandbox.

    Both are written into the sandbox directory, so the test module reaches the candidate as
    ``import candidate_rule`` — pytest's default prepend import mode puts the test file's own
    directory on ``sys.path``, which is what makes that import resolve without a ``PYTHONPATH`` the
    child is not given.

    Only pytest's own exit codes are interpreted: 0 passed, 1 tests failed, anything else means
    pytest could not deliver a verdict and the candidate stays unverified.
    """
    files = {RULE_MODULE_NAME: rule_source, TEST_MODULE_NAME: test_source}
    files.update(extra_files or {})
    result = run_files(
        files,
        # -p no:cacheprovider: the cache would be written into a directory about to be deleted.
        # -o addopts=: neutralises any pytest config the sandbox directory's ancestors happen to
        # carry, so a candidate's verdict does not depend on where the temp directory landed.
        ["-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", TEST_MODULE_NAME],
        timeout_seconds=timeout_seconds,
        memory_limit_bytes=memory_limit_bytes,
    )
    # Exit 1 is the only non-zero code that carries a verdict; it becomes FAILED. Everything else
    # stays CRASHED, including _PYTEST_NO_TESTS_COLLECTED — a suite that collected nothing verified
    # nothing, and calling that a failure would invite a caller to read it as "the rule is wrong"
    # rather than "the tests are missing".
    if result.outcome is SandboxOutcome.CRASHED and result.exit_code == _PYTEST_TESTS_FAILED:
        return _replace_outcome(result, SandboxOutcome.FAILED)
    return result


def run_files(
    files: Mapping[str, str],
    argv: Sequence[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
) -> SandboxResult:
    """Write ``files`` into a fresh temp directory and run ``sys.executable argv`` there.

    The low-level entry point. Returns ``PASSED`` on exit 0, ``TIMEOUT`` if the session had to be
    killed, and ``CRASHED`` for every other exit — classifying a non-zero code as a *test failure*
    needs knowledge of the tool being run, which is ``run_tests``'s business, not this function's.

    Raises ``ValueError`` for a caller mistake: an unusable filename, a non-positive timeout or
    memory limit, an empty ``argv``.
    """
    _validate_call(files, argv, timeout_seconds, memory_limit_bytes)

    workdir = Path(tempfile.mkdtemp(prefix="skej-sandbox-"))
    try:
        for name, content in files.items():
            (workdir / name).write_text(content, encoding="utf-8")
        return _spawn(
            [sys.executable, *_INTERPRETER_FLAGS, *argv],
            workdir,
            timeout_seconds=timeout_seconds,
            memory_limit_bytes=memory_limit_bytes,
        )
    finally:
        # Whatever the candidate left behind goes with the directory, including on the timeout path.
        shutil.rmtree(workdir, ignore_errors=True)


def _spawn(
    command: Sequence[str],
    workdir: Path,
    *,
    timeout_seconds: float,
    memory_limit_bytes: int,
) -> SandboxResult:
    """Run ``command`` in ``workdir`` under the sandbox's bounds and classify how it ended."""
    started = time.monotonic()
    process = subprocess.Popen(
        list(command),
        cwd=str(workdir),
        env=_child_env(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        # Its own session, so a timeout can kill anything the candidate spawned along with it.
        start_new_session=True,
        preexec_fn=_apply_limits(memory_limit_bytes),
    )

    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_session(process)
        # The pipes are drained after the kill so a child that filled them cannot deadlock the
        # cleanup; the process is already gone, so this returns rather than blocking.
        stdout, stderr = process.communicate()
        return SandboxResult(
            outcome=SandboxOutcome.TIMEOUT,
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started,
            timeout_seconds=timeout_seconds,
        )

    return SandboxResult(
        outcome=(SandboxOutcome.PASSED if process.returncode == 0 else SandboxOutcome.CRASHED),
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=time.monotonic() - started,
        timeout_seconds=timeout_seconds,
    )


def _kill_session(process: subprocess.Popen) -> None:
    """SIGKILL the child's whole process session, falling back to the child alone."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Already reaped, or a platform without process groups. Killing the child is still correct.
        process.kill()


def _apply_limits(memory_limit_bytes: int):
    """Build the ``preexec_fn`` that caps the child, or ``None`` where nothing can be capped.

    Runs in the forked child between ``fork`` and ``exec``, so it must do as little as possible and
    must not raise: an exception here surfaces as a failure to start the process at all.
    """
    if resource is None:  # pragma: no cover - Windows only
        return None

    def limit() -> None:  # pragma: no cover - executes in the child, after fork
        if hasattr(resource, "RLIMIT_AS"):
            try:
                resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
            except (ValueError, OSError):
                # A platform that will not accept the cap runs without it; the timeout still binds.
                pass
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass

    return limit


def _child_env(workdir: Path) -> dict[str, str]:
    """The child's entire environment. Nothing is inherited from this process.

    ``PATH`` is present because a subprocess of the candidate would otherwise resolve nothing — the
    interpreter itself is invoked by absolute path and does not need it. ``HOME`` and ``TMPDIR``
    point into the sandbox directory so anything written to either is deleted with the run.
    """
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
    }


def _validate_call(
    files: Mapping[str, str],
    argv: Sequence[str],
    timeout_seconds: float,
    memory_limit_bytes: int,
) -> None:
    """Reject a call this module cannot honour.

    Caller bugs raise here; candidate misbehaviour never does.
    """
    if not files:
        raise ValueError("Sandbox needs at least one file to write")
    for name in files:
        if not name or name != Path(name).name or name in {".", ".."}:
            raise ValueError(
                f"Sandbox filename {name!r} must be a plain filename with no path separators; "
                "files are written into the sandbox directory and nowhere else."
            )
    if not argv:
        raise ValueError("Sandbox needs a command to run")
    if timeout_seconds <= 0:
        raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds!r}")
    if memory_limit_bytes <= 0:
        raise ValueError(f"memory_limit_bytes must be positive, got {memory_limit_bytes!r}")


def _replace_outcome(result: SandboxResult, outcome: SandboxOutcome) -> SandboxResult:
    return SandboxResult(
        outcome=outcome,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_seconds=result.duration_seconds,
        timeout_seconds=result.timeout_seconds,
    )
