"""Tests for the subprocess sandbox.

The interesting cases are the ones where something goes wrong: the sandbox exists to turn a
candidate that hangs, crashes, or eats the machine into a *value* the generation loop can act on.
So most of what is asserted here is that nothing propagates out — no exception, no hang, no
lingering temp directory — and that the outcome is never ``PASSED`` unless the run genuinely was.

Timeouts in these tests are deliberately fractions of a second. A short timeout proves the same
mechanism as a long one and keeps the suite fast; nothing here sleeps for seconds to make a point.

``rules.sandbox.MEMORY_CAP_ENFORCED`` is not a sufficient guard for the memory test: macOS has
``RLIMIT_AS`` and accepts ``setrlimit`` on it, but does not reliably hold a process to it. That test
is therefore keyed on Linux by name rather than on the flag.
"""

import os
import sys
import textwrap
from pathlib import Path

import pytest

from rules.sandbox import (
    DEFAULT_TIMEOUT_SECONDS,
    RULE_MODULE_NAME,
    SandboxOutcome,
    run_files,
    run_module,
    run_tests,
)

#: The memory cap is only honoured on Linux; see the module docstring and ``sandbox``'s.
linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="RLIMIT_AS is only reliably enforced on Linux; macOS accepts the call and ignores it",
)


def src(text: str) -> str:
    """Dedent an inline source literal so it is legal Python once written to a file."""
    return textwrap.dedent(text).lstrip("\n")


# --------------------------------------------------------------------------------------------
# A clean run
# --------------------------------------------------------------------------------------------


def test_clean_module_run_passes_and_returns_output():
    result = run_module(src("print('rule loaded')\n"), timeout_seconds=10)

    assert result.outcome is SandboxOutcome.PASSED
    assert result.passed
    assert result.exit_code == 0
    assert "rule loaded" in result.stdout


def test_clean_test_run_passes_and_imports_the_candidate():
    rule = src("""
        class MaxDurationRule:
            def __init__(self, max_minutes):
                self.max_minutes = max_minutes

            def allows(self, minutes):
                return minutes <= self.max_minutes
        """)
    tests = src("""
        from candidate_rule import MaxDurationRule


        def test_within_limit():
            assert MaxDurationRule(60).allows(30)


        def test_over_limit():
            assert not MaxDurationRule(60).allows(90)
        """)

    result = run_tests(rule, tests, timeout_seconds=60)

    assert result.outcome is SandboxOutcome.PASSED, result.stdout + result.stderr
    assert "2 passed" in result.stdout


# --------------------------------------------------------------------------------------------
# Test failure — a verdict, distinct from a crash
# --------------------------------------------------------------------------------------------


def test_failing_test_is_reported_as_failed_not_crashed():
    rule = src("VALUE = 1\n")
    tests = src("""
        from candidate_rule import VALUE


        def test_value():
            assert VALUE == 2
        """)

    result = run_tests(rule, tests, timeout_seconds=60)

    assert result.outcome is SandboxOutcome.FAILED
    assert not result.passed
    assert "1 failed" in result.stdout


def test_suite_that_collects_nothing_is_a_crash_not_a_pass():
    """Fail closed: a test file with no tests in it has established nothing about the candidate."""
    result = run_tests(src("VALUE = 1\n"), src("VALUE = 1\n"), timeout_seconds=60)

    assert result.outcome is SandboxOutcome.CRASHED
    assert not result.passed


# --------------------------------------------------------------------------------------------
# Crashes are returned, never raised
# --------------------------------------------------------------------------------------------


def test_raising_module_is_reported_as_a_structured_result():
    result = run_module(src("raise RuntimeError('boom')\n"), timeout_seconds=10)

    assert result.outcome is SandboxOutcome.CRASHED
    assert not result.passed
    assert result.exit_code != 0
    assert "RuntimeError" in result.stderr
    assert "boom" in result.stderr


def test_unparseable_module_is_reported_as_a_structured_result():
    result = run_module(src("def broken(:\n"), timeout_seconds=10)

    assert result.outcome is SandboxOutcome.CRASHED
    assert "SyntaxError" in result.stderr


def test_module_exiting_nonzero_is_a_crash():
    result = run_module(src("import sys\nsys.exit(3)\n"), timeout_seconds=10)

    assert result.outcome is SandboxOutcome.CRASHED
    assert result.exit_code == 3


def test_collection_error_in_the_test_module_is_a_crash():
    """pytest's internal-error codes are not verdicts, so they must not read as a failure."""
    tests = src("import nonexistent_module_xyz\n\n\ndef test_nothing():\n    assert True\n")

    result = run_tests(src("VALUE = 1\n"), tests, timeout_seconds=60)

    assert result.outcome is SandboxOutcome.CRASHED


# --------------------------------------------------------------------------------------------
# Timeout
# --------------------------------------------------------------------------------------------


def test_infinite_loop_times_out():
    result = run_module(src("while True:\n    pass\n"), timeout_seconds=0.5)

    assert result.outcome is SandboxOutcome.TIMEOUT
    assert not result.passed
    assert result.exit_code is None
    assert result.timeout_seconds == 0.5
    # Generous, but far below the default: what is being proved is that the bound applied at all.
    assert result.duration_seconds < DEFAULT_TIMEOUT_SECONDS


def test_timeout_kills_a_process_the_candidate_spawned():
    """The whole session dies, not just the direct child.

    A grandchild holding the inherited stdout pipe open would otherwise keep the drain after the
    kill blocked until it exited on its own — the hang the timeout exists to prevent, relocated one
    process further away.
    """
    source = src("""
        import subprocess

        subprocess.Popen(["/bin/sleep", "60"])
        while True:
            pass
        """)

    result = run_module(source, timeout_seconds=0.5)

    assert result.outcome is SandboxOutcome.TIMEOUT
    assert result.duration_seconds < 20


# --------------------------------------------------------------------------------------------
# Memory cap
# --------------------------------------------------------------------------------------------


@linux_only
def test_memory_cap_is_enforced():
    # Well over the cap, and allocated in one go so the failure is a clean MemoryError rather than
    # the OOM killer arriving at some unrelated moment.
    source = src("data = bytearray(600 * 1024 * 1024)\nprint(len(data))\n")

    result = run_module(source, timeout_seconds=30, memory_limit_bytes=256 * 1024 * 1024)

    assert result.outcome is SandboxOutcome.CRASHED
    assert not result.passed
    assert "MemoryError" in result.stderr


@linux_only
def test_allocation_under_the_cap_still_passes():
    """The cap must bound a runaway, not ordinary work."""
    source = src("data = bytearray(16 * 1024 * 1024)\nprint(len(data))\n")

    result = run_module(source, timeout_seconds=30, memory_limit_bytes=512 * 1024 * 1024)

    assert result.outcome is SandboxOutcome.PASSED, result.stderr


# --------------------------------------------------------------------------------------------
# Isolation: environment, cwd, cleanup
# --------------------------------------------------------------------------------------------


def test_child_inherits_no_environment(monkeypatch):
    monkeypatch.setenv("SKEJ_SECRET_TOKEN", "do-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")

    source = src("""
        import os

        print("SECRET=%r" % os.environ.get("SKEJ_SECRET_TOKEN"))
        print("PYTHONPATH=%r" % os.environ.get("PYTHONPATH"))
        """)
    result = run_module(source, timeout_seconds=10)

    assert result.outcome is SandboxOutcome.PASSED, result.stderr
    assert "SECRET=None" in result.stdout
    assert "PYTHONPATH=None" in result.stdout


def test_child_runs_in_a_temp_dir_that_is_deleted_afterwards():
    source = src("""
        import os

        print(os.getcwd())
        open("scratch.txt", "w").write("left behind")
        """)
    result = run_module(source, timeout_seconds=10)

    assert result.outcome is SandboxOutcome.PASSED, result.stderr
    workdir = Path(result.stdout.strip().splitlines()[0])
    assert workdir.name.startswith("skej-sandbox-")
    assert workdir != Path.cwd()
    # The candidate's own leftovers go with the directory.
    assert not workdir.exists()


def test_temp_dir_is_deleted_after_a_timeout_too():
    source = src("""
        import os
        import sys

        print(os.getcwd(), flush=True)
        while True:
            pass
        """)
    result = run_module(source, timeout_seconds=0.5)

    assert result.outcome is SandboxOutcome.TIMEOUT
    workdir = Path(result.stdout.strip().splitlines()[0])
    assert not workdir.exists()


def test_repo_files_are_not_reachable_from_the_sandbox():
    """The sandbox directory is the candidate's world; the checkout it was launched from is not."""
    source = src("""
        import os

        print(sorted(os.listdir(".")))
        """)
    result = run_module(source, timeout_seconds=10)

    assert result.outcome is SandboxOutcome.PASSED, result.stderr
    assert result.stdout.strip() == repr([RULE_MODULE_NAME])


def test_extra_files_are_written_alongside_the_candidate():
    result = run_module(
        src("import helper\n\nprint(helper.VALUE)\n"),
        timeout_seconds=10,
        extra_files={"helper.py": "VALUE = 42\n"},
    )

    assert result.outcome is SandboxOutcome.PASSED, result.stderr
    assert "42" in result.stdout


# --------------------------------------------------------------------------------------------
# Caller mistakes raise; candidate misbehaviour does not
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../escape.py", "nested/rule.py", "/etc/passwd", "", "."],
)
def test_filename_outside_the_sandbox_directory_is_rejected(name):
    with pytest.raises(ValueError):
        run_files({name: "VALUE = 1\n"}, ["-c", "pass"], timeout_seconds=10)


def test_empty_file_set_is_rejected():
    with pytest.raises(ValueError):
        run_files({}, ["-c", "pass"], timeout_seconds=10)


def test_empty_argv_is_rejected():
    with pytest.raises(ValueError):
        run_files({RULE_MODULE_NAME: "VALUE = 1\n"}, [], timeout_seconds=10)


@pytest.mark.parametrize("timeout", [0, -1])
def test_non_positive_timeout_is_rejected(timeout):
    with pytest.raises(ValueError):
        run_module("VALUE = 1\n", timeout_seconds=timeout)


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_memory_limit_is_rejected(limit):
    with pytest.raises(ValueError):
        run_module("VALUE = 1\n", memory_limit_bytes=limit)


# --------------------------------------------------------------------------------------------
# The result object
# --------------------------------------------------------------------------------------------


def test_only_passed_reports_passed():
    """The one property a caller checks must be true for exactly one outcome."""
    clean = run_module(src("VALUE = 1\n"), timeout_seconds=10)
    crashed = run_module(src("raise SystemExit(2)\n"), timeout_seconds=10)
    timed_out = run_module(src("while True:\n    pass\n"), timeout_seconds=0.5)

    assert clean.passed
    assert not crashed.passed
    assert not timed_out.passed
    assert {r.outcome for r in (clean, crashed, timed_out)} == {
        SandboxOutcome.PASSED,
        SandboxOutcome.CRASHED,
        SandboxOutcome.TIMEOUT,
    }


def test_summary_is_loggable_for_every_outcome():
    for result in (
        run_module(src("VALUE = 1\n"), timeout_seconds=10),
        run_module(src("raise RuntimeError('x')\n"), timeout_seconds=10),
        run_module(src("while True:\n    pass\n"), timeout_seconds=0.5),
    ):
        assert isinstance(result.summary(), str)
        assert result.summary()


def test_sandbox_does_not_disturb_the_parent_environment():
    """Nothing here mutates the caller's process — the isolation is the child's, not ours."""
    before = dict(os.environ)

    run_module(src("VALUE = 1\n"), timeout_seconds=10)

    assert dict(os.environ) == before
