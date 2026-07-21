"""Making the engine types reachable inside the sandbox.

A generated rule opens with ``class MaxDurationRule(BaseRule):`` and imports nothing to get
``BaseRule``. That is not an oversight: ``rules`` is not on the safety validator's import allowlist,
and widening it would readmit the whole package as a capability, so the engine types are **free
names** that whatever loads the source is expected to bind.

The sandbox binds nothing. It gives the child no ``PYTHONPATH`` and a curated environment, which is
the entire point of it. So a candidate run there as-is dies at import with ``NameError: name
'BaseRule' is not defined`` — and because that surfaces as ``CRASHED`` rather than ``FAILED``, a
retry loop that did not solve this would spend its whole budget on every rule and give up every
time, while looking from the outside like it was working.

This module closes that gap. ``rules/rules/interfaces.py`` imports only the standard library
(``abc``, ``dataclasses``, ``datetime``, ``enum``) and is entirely self-contained, so it can be
copied into the sandbox directory as ``engine.py`` and imported there like any other local module.
``PRELUDE`` is then prepended to the candidate so its free names resolve.

**The prelude binds exactly the six names the generator's system prompt promises, and no more.** It
is the executable statement of that promise. Binding a seventh here would let a candidate that used
it pass in the sandbox and then fail wherever it is really loaded, which is the one failure this
whole arrangement exists to prevent. The test module, which is a different kind of code with
different needs, imports whatever it likes from ``engine`` directly.

**Ordering — do not "tidy" this.** ``validate_source`` runs on the *generated source alone*, inside
``generate_rule``, before this module ever sees it. The prelude is prepended **afterwards**.
Validating the assembled module instead would reject it on its first line: ``engine`` is not on the
import allowlist, which permits only ``datetime``, ``zoneinfo`` and ``math``. That change looks like
a tightening and is a total outage of rule generation.

A validated candidate cannot itself begin with ``from __future__ import ...`` — ``__future__`` is
not on the import allowlist either, so the validator has already rejected it — which is what makes
plain prepending safe. If one ever did get through, the child would report a syntax error, the run
would be ``CRASHED``, and the loop would feed that back: wrong, but wrong in the fail-closed
direction.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from rules import interfaces, sandbox

__all__ = [
    "ENGINE_MODULE_NAME",
    "ENGINE_NAMES",
    "PRELUDE",
    "assemble_candidate_module",
    "engine_source",
    "sandbox_files",
    "run_candidate",
]

#: What ``interfaces.py`` is called inside the sandbox. The test module imports from it by this
#: name, so it is stated in the Tester's system prompt and cannot change without changing that too.
ENGINE_MODULE_NAME = "engine.py"

#: The free names a generated rule may use without importing them. This tuple and the "Hard
#: constraints" section of the Generator's system prompt say the same thing in two languages; they
#: are meant to stay in step.
ENGINE_NAMES = (
    "BaseRule",
    "BookingRecord",
    "BookingRequest",
    "Context",
    "RuleResult",
    "Weekday",
)

PRELUDE = (
    "# Prepended by rules/generation/harness.py AFTER the candidate passed validate_source.\n"
    "# It binds the free names the rule contract gives a rule; see that module for why the\n"
    "# validator must never run over this line.\n"
    f"from {Path(ENGINE_MODULE_NAME).stem} import (\n"
    + "".join(f"    {name},\n" for name in ENGINE_NAMES)
    + ")\n\n"
)


@lru_cache(maxsize=1)
def engine_source() -> str:
    """The text of ``rules/rules/interfaces.py``, to be shipped into the sandbox as ``engine.py``.

    Read from the installed module rather than kept as a copy here, so the contract the sandbox
    enforces is the contract the application runs. A copy would drift, and it would drift silently:
    every candidate would still pass against the stale one.
    """
    return Path(interfaces.__file__).read_text(encoding="utf-8")


def assemble_candidate_module(rule_source: str) -> str:
    """Return ``rule_source`` with the prelude prepended, ready to run in the sandbox.

    Takes source that has **already** been through ``validate_source``. See the module docstring:
    validating the return value of this function instead would reject every candidate.
    """
    if not isinstance(rule_source, str):
        raise TypeError(f"rule_source must be a str, got {type(rule_source).__name__}")
    return PRELUDE + rule_source


def sandbox_files() -> dict[str, str]:
    """The extra files every candidate run needs alongside the rule and its tests."""
    return {ENGINE_MODULE_NAME: engine_source()}


def run_candidate(
    rule_source: str,
    test_source: str,
    *,
    timeout_seconds: float = sandbox.DEFAULT_TIMEOUT_SECONDS,
    memory_limit_bytes: int = sandbox.DEFAULT_MEMORY_LIMIT_BYTES,
) -> sandbox.SandboxResult:
    """Run ``test_source`` against ``rule_source`` in the sandbox, with the engine shipped in.

    The one way this package executes a candidate. Everything the child needs — the assembled
    module, the test file, ``engine.py`` — is decided here rather than at each call site, so no
    caller can run a candidate that is missing the piece that makes it loadable.
    """
    return sandbox.run_tests(
        assemble_candidate_module(rule_source),
        test_source,
        timeout_seconds=timeout_seconds,
        memory_limit_bytes=memory_limit_bytes,
        extra_files=sandbox_files(),
    )
