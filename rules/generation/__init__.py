"""The AI generation loop: a developer tool that writes rule source for a human to review.

A sibling package of ``rules`` rather than part of it. ``rules`` is what the booking API imports and
runs in-process; this is what a developer runs at a terminal to produce a candidate. Keeping them
apart is what makes "nothing generated is imported by the app" a property of the layout rather than
a promise: the engine has no reason to import this package, and does not.
"""

from .errors import GenerationError, LLMCallError, RuleRejectedError, SuiteRejectedError
from .generator import SYSTEM_PROMPT, build_prompt, generate_rule, strip_code_fence
from .harness import (
    ENGINE_MODULE_NAME,
    ENGINE_NAMES,
    PRELUDE,
    assemble_candidate_module,
    engine_source,
    run_candidate,
    sandbox_files,
)
from .llm import (
    DEFAULT_CLI_EXECUTABLE,
    DEFAULT_CLI_TIMEOUT_SECONDS,
    DEFAULT_MODEL,
    ClaudeCliClient,
    LLMClient,
    LLMResponse,
    build_command,
    interpret_cli_result,
)
from .loop import (
    DEFAULT_OUTPUT_DIR,
    MAX_RETRIES,
    Attempt,
    AttemptOutcome,
    LoopResult,
    run_generation_loop,
    write_artifact,
)
from .tester import TESTER_SYSTEM_PROMPT, build_test_prompt, generate_tests

__all__ = [
    "GenerationError",
    "LLMCallError",
    "RuleRejectedError",
    "SuiteRejectedError",
    "generate_rule",
    "build_prompt",
    "strip_code_fence",
    "SYSTEM_PROMPT",
    "generate_tests",
    "build_test_prompt",
    "TESTER_SYSTEM_PROMPT",
    "run_generation_loop",
    "LoopResult",
    "Attempt",
    "AttemptOutcome",
    "MAX_RETRIES",
    "DEFAULT_OUTPUT_DIR",
    "write_artifact",
    "ENGINE_MODULE_NAME",
    "ENGINE_NAMES",
    "PRELUDE",
    "assemble_candidate_module",
    "engine_source",
    "sandbox_files",
    "run_candidate",
    "LLMClient",
    "LLMResponse",
    "ClaudeCliClient",
    "build_command",
    "interpret_cli_result",
    "DEFAULT_MODEL",
    "DEFAULT_CLI_EXECUTABLE",
    "DEFAULT_CLI_TIMEOUT_SECONDS",
]
