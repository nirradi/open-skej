"""The AI generation loop: it writes rule source, and something else decides what to do with it.

A sibling package of ``rules`` rather than part of it. ``rules`` is what the booking API imports and
runs in-process; this package produces candidates for it — at a developer's terminal via
``rules/benchmark.py``, and since task 7.7 inside the backend as a job
(``app/backend/app/rule_generation.py``).

That second caller ends the arrangement this docstring used to describe. Keeping the packages apart
was what made "nothing generated is imported by the app" a property of the layout; the backend now
imports ``generation.harness`` for its catalog and ``generation.loop`` for the job runner, so the
guarantee is gone and four properties replace it, none of which depend on the packaging:

* ``rules.safety.validate_source`` runs over the source at **every load**, not only at write time;
* a hoisted rule executes in a namespace whose builtins are ``rules.safety.SAFE_BUILTINS``;
* the adversarial suite must pass in the sandbox before a row is ever stored;
* the controller's containment wraps every call a hoisted rule makes thereafter.

``generation`` carries no dependencies of its own — standard library only — so having the backend
install it does not move the booking API's dependency set.
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
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    ClaudeCliClient,
    LLMClient,
    LLMResponse,
    OllamaClient,
    build_chat_request,
    build_command,
    interpret_cli_result,
    interpret_ollama_result,
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
    "OllamaClient",
    "build_chat_request",
    "interpret_ollama_result",
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_OLLAMA_TIMEOUT_SECONDS",
]
