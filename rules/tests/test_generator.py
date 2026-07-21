"""Tests for Agent A and the LLM seam.

**No test here calls a model, and none runs the ``claude`` binary.** The client is a fake that
returns whatever the test hands it, and ``ClaudeCliClient`` is exercised through the two pure
functions it is built out of — ``build_command`` (what would be run) and ``interpret_cli_result``
(what was returned). The JSON in ``SUCCESS_PAYLOAD`` and ``MODEL_404_PAYLOAD`` is *captured* from
real invocations rather than invented, which is the only way a parser test is worth anything: the
detail that matters most below — a failed call reporting ``subtype: "success"`` alongside
``is_error: true`` — is one nobody would have guessed.

The captured success payload is also the evidence for the CLI's overhead: 10 input and 39 output
tokens of actual prompt, 11,567 read from cache plus 6,222 written to it, $0.0144 for the call, and
a second model in ``modelUsage`` nothing here asked for.
"""

import json
import textwrap

import pytest

from generation.errors import GenerationError, LLMCallError, RuleRejectedError
from generation.generator import (
    SYSTEM_PROMPT,
    build_prompt,
    generate_rule,
    strip_code_fence,
)
from generation.llm import (
    DEFAULT_MODEL,
    ClaudeCliClient,
    LLMResponse,
    build_command,
    interpret_cli_result,
)

# --------------------------------------------------------------------------------------------
# Fixtures and fakes
# --------------------------------------------------------------------------------------------

#: A candidate that survives the safety validator: no imports of the engine, parameters on the
#: instance, no dunder attribute access, no decorators. What a good generation looks like.
GOOD_RULE = textwrap.dedent('''\
    class MaxBookingsPerWeekRule(BaseRule):
        """At most ``max_bookings`` bookings in the week the request falls in. Inclusive bound."""

        def __init__(self, max_bookings):
            if max_bookings <= 0:
                raise ValueError(f"max_bookings must be positive; got {max_bookings!r}")
            self.max_bookings = max_bookings

        def evaluate(self, request, context):
            start = request.start_at - timedelta(days=request.start_at.weekday())
            lower = start.replace(hour=0, minute=0, second=0, microsecond=0)
            upper = lower + timedelta(days=7)
            existing = sum(
                1 for booking in context.history.bookings if lower <= booking.start_at < upper
            )
            if existing + 1 > self.max_bookings:
                return RuleResult.deny(
                    "You can make at most 2 bookings a week. Please pick another week."
                )
            return RuleResult.allow()
    ''')


class FakeClient:
    """An ``LLMClient`` that returns a canned completion and records how it was called."""

    def __init__(self, text: str = GOOD_RULE) -> None:
        self.text = text
        self.calls: list[dict[str, str]] = []

    def complete(self, *, system: str, prompt: str, model: str = DEFAULT_MODEL) -> LLMResponse:
        self.calls.append({"system": system, "prompt": prompt, "model": model})
        return LLMResponse(text=self.text, model=model)


class ExplodingClient:
    """An ``LLMClient`` whose backend is unreachable — the CLI missing, the session unauthed."""

    def complete(self, *, system: str, prompt: str, model: str = DEFAULT_MODEL) -> LLMResponse:
        raise LLMCallError("claude is not on PATH", exit_code=127, stderr="command not found")


# --------------------------------------------------------------------------------------------
# Fence stripping
# --------------------------------------------------------------------------------------------


def test_strips_a_python_tagged_fence():
    assert strip_code_fence("```python\nclass R:\n    pass\n```") == "class R:\n    pass"


def test_strips_a_bare_fence():
    assert strip_code_fence("```\nclass R:\n    pass\n```") == "class R:\n    pass"


def test_strips_a_py_tagged_fence():
    assert strip_code_fence("```py\nclass R:\n    pass\n```") == "class R:\n    pass"


def test_unfenced_source_is_returned_as_is():
    assert strip_code_fence("class R:\n    pass\n") == "class R:\n    pass"


def test_prose_before_the_fence_is_discarded():
    text = "Here is the rule you asked for:\n\n```python\nclass R:\n    pass\n```\n\nHope it helps!"
    assert strip_code_fence(text) == "class R:\n    pass"


def test_an_unterminated_fence_yields_the_body_it_has():
    # A cut-off answer. Returning the truncated body gets a parse-failure rejection the loop can
    # feed back; returning the raw text would fail on the stray fence instead.
    assert strip_code_fence("```python\nclass R:\n    pas") == "class R:\n    pas"


def test_indented_fence_is_recognised():
    assert strip_code_fence("  ```python\n  class R:\n      pass\n  ```").startswith("class R:")


def test_fence_stripping_preserves_inner_indentation():
    source = strip_code_fence(f"```python\n{GOOD_RULE}```")
    assert "    def evaluate(self, request, context):" in source


# --------------------------------------------------------------------------------------------
# generate_rule
# --------------------------------------------------------------------------------------------


def test_returns_validated_source_for_a_good_candidate():
    client = FakeClient(f"```python\n{GOOD_RULE}```")
    assert generate_rule("max 2 bookings a week", client=client) == GOOD_RULE.strip()


def test_description_reaches_the_prompt():
    client = FakeClient()
    generate_rule("only on weekends", client=client)
    assert "only on weekends" in client.calls[0]["prompt"]


def test_system_prompt_is_sent():
    client = FakeClient()
    generate_rule("max 1 hour", client=client)
    assert client.calls[0]["system"] == SYSTEM_PROMPT


def test_default_model_is_opus():
    client = FakeClient()
    generate_rule("max 1 hour", client=client)
    assert client.calls[0]["model"] == "claude-opus-4-8"


def test_model_is_threaded_through_to_the_client():
    client = FakeClient()
    generate_rule("max 1 hour", client=client, model="claude-haiku-4-5")
    assert client.calls[0]["model"] == "claude-haiku-4-5"


def test_blank_description_is_a_caller_error_and_never_reaches_the_model():
    client = FakeClient()
    with pytest.raises(ValueError):
        generate_rule("   ", client=client)
    assert client.calls == []


def test_a_backend_failure_surfaces_structurally():
    with pytest.raises(LLMCallError) as excinfo:
        generate_rule("max 1 hour", client=ExplodingClient())
    assert excinfo.value.exit_code == 127
    assert "command not found" in excinfo.value.stderr
    assert isinstance(excinfo.value, GenerationError)


def test_an_engine_import_is_rejected_with_the_validators_reason():
    candidate = "from rules.interfaces import BaseRule\n\n\nclass R(BaseRule):\n    pass\n"
    with pytest.raises(RuleRejectedError) as excinfo:
        generate_rule("max 1 hour", client=FakeClient(candidate))
    assert "rules" in excinfo.value.reason
    assert "not allowed" in excinfo.value.reason
    assert excinfo.value.source == candidate.strip()


def test_a_forbidden_import_is_rejected():
    with pytest.raises(RuleRejectedError) as excinfo:
        generate_rule("max 1 hour", client=FakeClient("import os\n\n\nclass R:\n    pass\n"))
    assert "'os'" in excinfo.value.reason


def test_dunder_attribute_access_is_rejected():
    candidate = "class R(BaseRule):\n    def evaluate(self, request, context):\n        return ().__class__\n"  # noqa: E501
    with pytest.raises(RuleRejectedError) as excinfo:
        generate_rule("max 1 hour", client=FakeClient(candidate))
    assert "__class__" in excinfo.value.reason


def test_unparseable_source_is_rejected_not_raised_as_syntax_error():
    with pytest.raises(RuleRejectedError) as excinfo:
        generate_rule("max 1 hour", client=FakeClient("class R(:\n"))
    assert "does not parse" in excinfo.value.reason


def test_an_empty_completion_is_rejected():
    # An empty AST is trivially "safe", so the validator alone would let this through as a rule.
    with pytest.raises(RuleRejectedError) as excinfo:
        generate_rule("max 1 hour", client=FakeClient("```python\n```"))
    assert "no Python source" in excinfo.value.reason


def test_rejection_and_call_failure_share_a_base():
    assert issubclass(RuleRejectedError, GenerationError)
    assert issubclass(LLMCallError, GenerationError)


def test_build_prompt_delimits_the_untrusted_description():
    prompt = build_prompt("max 1 hour")
    assert "<constraint>\nmax 1 hour\n</constraint>" in prompt


# --------------------------------------------------------------------------------------------
# The system prompt states the constraints that are real failure modes
# --------------------------------------------------------------------------------------------


def test_system_prompt_forbids_importing_the_engine():
    assert "DO NOT IMPORT ANYTHING FROM THE RULE ENGINE" in SYSTEM_PROMPT
    assert "FREE NAMES" in SYSTEM_PROMPT
    assert "from rules.interfaces import BaseRule" in SYSTEM_PROMPT


def test_system_prompt_names_the_whole_import_allowlist():
    for module in ("datetime", "zoneinfo", "math"):
        assert module in SYSTEM_PROMPT


def test_system_prompt_demands_datetime_be_imported_not_assumed_free():
    """The engine types are free names; ``timedelta`` is not, and a model conflates the two.

    Observed against a live model: told only that the engine types are free names, it emitted
    ``window=timedelta(days=7)`` as a default argument with no import. ``validate_source`` is a
    syntax check and passes that happily — the candidate then dies with ``NameError`` the instant it
    loads, spending one of three retries on an import almost every rule needs. Naming the failure in
    the prompt is what stops it, so this pins the instruction itself.
    """
    assert "from datetime import timedelta" in SYSTEM_PROMPT
    assert "must import" in SYSTEM_PROMPT
    assert "NameError" in SYSTEM_PROMPT


def test_system_prompt_states_the_fail_closed_policy():
    assert "FAIL CLOSED" in SYSTEM_PROMPT
    assert "Never catch your own exception and return a pass" in SYSTEM_PROMPT
    assert "never return anything that is not a RuleResult" in SYSTEM_PROMPT


def test_system_prompt_requires_parameters_on_the_instance():
    assert "PARAMETERS GO ON THE INSTANCE" in SYSTEM_PROMPT
    assert "never as module-level constants" in SYSTEM_PROMPT


def test_system_prompt_states_utc_and_forbids_dst_handling():
    assert "EVERY DATETIME IS UTC" in SYSTEM_PROMPT
    assert "DST" in SYSTEM_PROMPT


def test_system_prompt_states_that_all_history_counts():
    assert "EVERYTHING IN `context.history.bookings` COUNTS" in SYSTEM_PROMPT
    assert "no status field" in SYSTEM_PROMPT


def test_system_prompt_lists_the_remaining_rejected_constructs():
    for construct in ("decorator", "while", "super().__init__()", "global"):
        assert construct in SYSTEM_PROMPT


# --------------------------------------------------------------------------------------------
# ClaudeCliClient — argv and payload parsing, never the binary
# --------------------------------------------------------------------------------------------

#: Captured verbatim from `claude -p "Reply with exactly: OK" --model claude-haiku-4-5
#: --output-format json --allowedTools "" --max-turns 1`, trimmed of the session identifiers.
SUCCESS_PAYLOAD = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "api_error_status": None,
        "duration_ms": 1469,
        "duration_api_ms": 2911,
        "num_turns": 1,
        "result": "OK",
        "stop_reason": "end_turn",
        "total_cost_usd": 0.0143917,
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 6222,
            "cache_read_input_tokens": 11567,
            "output_tokens": 39,
            "service_tier": "standard",
        },
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"inputTokens": 521, "costUSD": 0.000586},
            "claude-haiku-4-5": {"inputTokens": 10, "costUSD": 0.0138057},
        },
        "permission_denials": [],
    }
)

#: Captured from the same command with an unknown model id. Note ``subtype`` is still "success".
MODEL_404_PAYLOAD = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": 404,
        "duration_ms": 817,
        "num_turns": 1,
        "result": (
            "There's an issue with the selected model (no-such-model-xyz). It may not exist or "
            "you may not have access to it. Run --model to pick a different model."
        ),
        "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "modelUsage": {},
    }
)


def test_build_command_carries_the_flags_that_make_it_a_completion():
    command = build_command("write a rule", model="claude-opus-4-8", system="be terse")
    assert command[:3] == ["claude", "-p", "write a rule"]
    assert command[command.index("--model") + 1] == "claude-opus-4-8"
    assert command[command.index("--output-format") + 1] == "json"
    assert command[command.index("--max-turns") + 1] == "1"
    # Variadic and empty: the session gets no tools at all.
    assert command[command.index("--allowedTools") + 1] == ""
    assert command[command.index("--system-prompt") + 1] == "be terse"


def test_build_command_omits_the_system_prompt_when_there_is_none():
    assert "--system-prompt" not in build_command("hi", model="m")


def test_build_command_honours_a_custom_executable():
    assert build_command("hi", model="m", executable="/opt/claude")[0] == "/opt/claude"


def test_client_rejects_a_non_positive_timeout():
    with pytest.raises(ValueError):
        ClaudeCliClient(timeout_seconds=0)


def test_success_payload_yields_text_and_metadata():
    response = interpret_cli_result(
        exit_code=0, stdout=SUCCESS_PAYLOAD, stderr="", model="claude-haiku-4-5"
    )
    assert response.text == "OK"
    assert response.model == "claude-haiku-4-5"
    assert response.input_tokens == 10
    assert response.output_tokens == 39
    assert response.cost_usd == pytest.approx(0.0143917)
    assert response.duration_ms == 1469


def test_raw_payload_is_kept_for_fields_the_dataclass_does_not_model():
    response = interpret_cli_result(
        exit_code=0, stdout=SUCCESS_PAYLOAD, stderr="", model="claude-haiku-4-5"
    )
    # The harness overhead the benchmark cannot see past, reachable but deliberately not modelled.
    assert response.raw["usage"]["cache_read_input_tokens"] == 11567
    assert "claude-haiku-4-5-20251001" in response.raw["modelUsage"]


def test_is_error_is_a_failure_even_though_subtype_says_success():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_cli_result(
            exit_code=1, stdout=MODEL_404_PAYLOAD, stderr="", model="no-such-model-xyz"
        )
    # The CLI's own account of the failure survives into the message.
    assert "no-such-model-xyz" in excinfo.value.detail
    assert "404" in excinfo.value.detail
    assert excinfo.value.exit_code == 1


def test_is_error_on_a_zero_exit_is_still_a_failure():
    with pytest.raises(LLMCallError):
        interpret_cli_result(exit_code=0, stdout=MODEL_404_PAYLOAD, stderr="", model="m")


def test_a_non_zero_exit_with_a_clean_payload_is_still_a_failure():
    with pytest.raises(LLMCallError):
        interpret_cli_result(exit_code=1, stdout=SUCCESS_PAYLOAD, stderr="", model="m")


def test_non_json_stdout_is_a_structured_failure():
    with pytest.raises(LLMCallError) as excinfo:
        interpret_cli_result(exit_code=1, stdout="Error: not logged in\n", stderr="boom", model="m")
    assert "did not emit JSON" in excinfo.value.detail
    assert "not logged in" in excinfo.value.detail
    assert excinfo.value.stderr == "boom"


def test_empty_stdout_is_a_structured_failure():
    with pytest.raises(LLMCallError):
        interpret_cli_result(exit_code=0, stdout="", stderr="", model="m")


def test_json_that_is_not_an_object_is_a_structured_failure():
    with pytest.raises(LLMCallError):
        interpret_cli_result(exit_code=0, stdout="[1, 2, 3]", stderr="", model="m")


def test_a_success_with_no_result_text_is_a_structured_failure():
    payload = json.dumps({"is_error": False, "subtype": "success", "usage": {}})
    with pytest.raises(LLMCallError) as excinfo:
        interpret_cli_result(exit_code=0, stdout=payload, stderr="", model="m")
    assert "no 'result' text" in excinfo.value.detail


def test_missing_metadata_is_none_rather_than_zero():
    payload = json.dumps({"is_error": False, "result": "hello"})
    response = interpret_cli_result(exit_code=0, stdout=payload, stderr="", model="m")
    assert response.text == "hello"
    assert response.input_tokens is None
    assert response.output_tokens is None
    assert response.cost_usd is None
    assert response.duration_ms is None


def test_a_captured_success_can_drive_generate_rule_end_to_end():
    """The two halves meet: a CLI payload carrying fenced source becomes validated rule source."""

    class ReplayClient:
        def complete(self, *, system, prompt, model=DEFAULT_MODEL):
            payload = json.dumps(
                {"is_error": False, "result": f"```python\n{GOOD_RULE}```", "duration_ms": 4200}
            )
            return interpret_cli_result(exit_code=0, stdout=payload, stderr="", model=model)

    assert generate_rule("max 2 a week", client=ReplayClient()) == GOOD_RULE.strip()
