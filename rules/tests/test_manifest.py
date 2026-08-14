"""Tests for Agent C: the manifest call made after a candidate has already passed.

No test here calls a model — every one drives a fake ``LLMClient`` that returns whatever text the
test hands it, the same shape ``test_generator.py`` and ``test_tester.py`` use for the other two
agents. What is real is the cross-check: ``_load_rule_class`` really execs the rule source handed
to it and really reads ``inspect.signature`` off the result, so a test asserting a mismatch is
rejected is asserting against the genuine constructor, not a mocked one.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from generation.errors import GenerationError, LLMCallError, ManifestRejectedError
from generation.llm import LLMResponse
from generation.manifest import (
    SYSTEM_PROMPT,
    RuleManifest,
    build_manifest_prompt,
    generate_manifest,
)

# --------------------------------------------------------------------------------------------
# Fixtures and fakes
# --------------------------------------------------------------------------------------------

#: A verified candidate with one integer-shaped constructor argument.
ONE_PARAM_RULE = textwrap.dedent('''\
    from datetime import timedelta


    class MaxDurationRule(BaseRule):
        """Bookings may not run longer than ``max_duration_minutes``."""

        def __init__(self, max_duration_minutes):
            if max_duration_minutes <= 0:
                raise ValueError("max_duration_minutes must be positive")
            self.max_duration_minutes = max_duration_minutes

        def evaluate(self, request, context):
            if request.duration > timedelta(minutes=self.max_duration_minutes):
                return RuleResult.deny("Too long. Please shorten it and try again.")
            return RuleResult.allow()
    ''')

#: A verified candidate that takes no constructor arguments at all.
NO_PARAM_RULE = textwrap.dedent("""\
    class NoWeekendBookingsRule(BaseRule):
        def evaluate(self, request, context):
            if context.local.weekday >= 5:
                return RuleResult.deny("No bookings on weekends.")
            return RuleResult.allow()
    """)

#: A verified candidate that genuinely reads history — the case that should stay `reads_history`.
READS_HISTORY_RULE = textwrap.dedent("""\
    class MaxTwiceAWeekRule(BaseRule):
        def __init__(self, max_bookings):
            self.max_bookings = max_bookings

        def evaluate(self, request, context):
            existing = [
                b for b in context.history.bookings
                if context.local.week_start <= b.start_at < context.local.week_end
            ]
            if len(existing) + 1 > self.max_bookings:
                return RuleResult.deny("Too many this week.")
            return RuleResult.allow()
    """)


#: A verified candidate that reads only `context.run`, never `context.history` — the false
#: negative task 8.8 closes. Its run is resolved from history before it ever runs
#: (`RunContext`, `.claude/rules/rule-engine.md`, "It resolves the run"), the identical shape
#: `max_consecutive_duration` is registered with (`rules/rules/registry.py`'s module docstring).
READS_RUN_RULE = textwrap.dedent("""\
    class MaxConsecutivePlayRule(BaseRule):
        def __init__(self, max_minutes):
            self.max_minutes = max_minutes

        def evaluate(self, request, context):
            run_minutes = context.run.duration.total_seconds() / 60
            if run_minutes > self.max_minutes:
                return RuleResult.deny("Too much play back to back.")
            return RuleResult.allow()
    """)


def _manifest_json(**overrides) -> str:
    payload = {
        "label": "Maximum duration",
        "description": "Caps how long a single booking may run.",
        "params": [
            {
                "name": "max_duration_minutes",
                "kind": "integer",
                "label": "Maximum duration",
                "unit": "minutes",
                "required": True,
                "minimum": 1,
            }
        ],
        "reads_history": False,
    }
    payload.update(overrides)
    return json.dumps(payload)


class FakeClient:
    """An ``LLMClient`` that returns a canned completion and records how it was called."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, str]] = []

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        self.calls.append({"system": system, "prompt": prompt, "model": model})
        return LLMResponse(text=self.text, model=model)


class ExplodingClient:
    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        raise LLMCallError("the CLI is not on PATH", exit_code=127)


# --------------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------------


def test_a_matching_manifest_is_accepted():
    manifest = generate_manifest(ONE_PARAM_RULE, "max 2 hours", client=FakeClient(_manifest_json()))

    assert manifest == RuleManifest(
        label="Maximum duration",
        description="Caps how long a single booking may run.",
        params=manifest.params,
        reads_history=False,
    )
    assert [p.name for p in manifest.params] == ["max_duration_minutes"]
    assert manifest.params[0].kind.value == "integer"


def test_a_manifest_fenced_in_markdown_is_still_parsed():
    client = FakeClient(f"```json\n{_manifest_json()}\n```")
    manifest = generate_manifest(ONE_PARAM_RULE, "max 2 hours", client=client)
    assert manifest.label == "Maximum duration"


def test_a_rule_with_no_constructor_arguments_gets_an_empty_param_list():
    manifest = generate_manifest(
        NO_PARAM_RULE,
        "no weekend bookings",
        client=FakeClient(_manifest_json(params=[])),
    )
    assert manifest.params == ()


def test_the_system_prompt_is_sent():
    client = FakeClient(_manifest_json())
    generate_manifest(ONE_PARAM_RULE, "max 2 hours", client=client)
    assert client.calls[0]["system"] == SYSTEM_PROMPT


def test_the_model_is_threaded_through():
    client = FakeClient(_manifest_json())
    generate_manifest(ONE_PARAM_RULE, "max 2 hours", client=client, model="claude-haiku-4-5")
    assert client.calls[0]["model"] == "claude-haiku-4-5"


def test_build_manifest_prompt_delimits_both_the_constraint_and_the_rule():
    prompt = build_manifest_prompt(ONE_PARAM_RULE, "max 2 hours")
    assert "<constraint>\nmax 2 hours\n</constraint>" in prompt
    assert "<rule>" in prompt
    assert "class MaxDurationRule(BaseRule):" in prompt


# --------------------------------------------------------------------------------------------
# reads_history: corrected against the source, never trusted from the model
# --------------------------------------------------------------------------------------------


def test_reads_history_true_on_a_source_that_never_touches_history_is_corrected_to_false():
    manifest = generate_manifest(
        ONE_PARAM_RULE,
        "max 2 hours",
        client=FakeClient(_manifest_json(reads_history=True)),
    )
    assert manifest.reads_history is False


def test_reads_history_true_on_a_source_that_does_touch_history_stays_true():
    manifest = generate_manifest(
        READS_HISTORY_RULE,
        "max 2 a week",
        client=FakeClient(
            _manifest_json(
                params=[
                    {
                        "name": "max_bookings",
                        "kind": "integer",
                        "label": "Max bookings",
                        "unit": "bookings",
                        "required": True,
                        "minimum": 1,
                    }
                ],
                reads_history=True,
            )
        ),
    )
    assert manifest.reads_history is True


def test_reads_history_true_on_a_source_that_reads_only_context_run_stays_true():
    """The gap task 8.8 closes: a rule reading only `context.run` never spells "history" anywhere
    in its own source, so the old substring check would zero out an honest `true` claim from the
    model and hand the rule an empty history — silently permissive, the direction this codebase
    never accepts (`rules/rules/registry.py`'s module docstring on `max_consecutive_duration`).
    """
    manifest = generate_manifest(
        READS_RUN_RULE,
        "no more than two hours of play in a row",
        client=FakeClient(
            _manifest_json(
                params=[
                    {
                        "name": "max_minutes",
                        "kind": "integer",
                        "label": "Max consecutive minutes",
                        "unit": "minutes",
                        "required": True,
                        "minimum": 1,
                    }
                ],
                reads_history=True,
            )
        ),
    )
    assert manifest.reads_history is True


def test_reads_history_false_is_never_elevated_to_true():
    manifest = generate_manifest(
        READS_HISTORY_RULE,
        "max 2 a week",
        client=FakeClient(
            _manifest_json(
                params=[
                    {
                        "name": "max_bookings",
                        "kind": "integer",
                        "label": "Max bookings",
                        "unit": "bookings",
                        "required": True,
                        "minimum": 1,
                    }
                ],
                reads_history=False,
            )
        ),
    )
    assert manifest.reads_history is False


# --------------------------------------------------------------------------------------------
# The cross-check: names must match inspect.signature exactly
# --------------------------------------------------------------------------------------------


def test_a_missing_param_is_rejected():
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(
            ONE_PARAM_RULE, "max 2 hours", client=FakeClient(_manifest_json(params=[]))
        )
    assert "missing" in excinfo.value.reason
    assert "max_duration_minutes" in excinfo.value.reason


def test_an_extra_param_is_rejected():
    extra_params = json.loads(_manifest_json())["params"] + [
        {
            "name": "not_a_real_argument",
            "kind": "integer",
            "label": "Nope",
            "unit": None,
            "required": True,
            "minimum": 1,
        }
    ]
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(
            ONE_PARAM_RULE,
            "max 2 hours",
            client=FakeClient(_manifest_json(params=extra_params)),
        )
    assert "extra" in excinfo.value.reason
    assert "not_a_real_argument" in excinfo.value.reason


def test_a_renamed_param_is_rejected_as_both_missing_and_extra():
    wrong_name = json.loads(_manifest_json())["params"]
    wrong_name[0]["name"] = "limit"
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(
            ONE_PARAM_RULE,
            "max 2 hours",
            client=FakeClient(_manifest_json(params=wrong_name)),
        )
    assert "missing" in excinfo.value.reason
    assert "extra" in excinfo.value.reason


def test_a_matching_param_set_in_a_different_order_is_still_accepted():
    """The cross-check compares the set of names, not their order."""
    two_param_rule = textwrap.dedent("""\
        class R(BaseRule):
            def __init__(self, first, second):
                self.first = first
                self.second = second

            def evaluate(self, request, context):
                return RuleResult.allow()
        """)
    manifest_text = _manifest_json(
        params=[
            {
                "name": "second",
                "kind": "integer",
                "label": "Second",
                "unit": None,
                "required": True,
                "minimum": None,
            },
            {
                "name": "first",
                "kind": "integer",
                "label": "First",
                "unit": None,
                "required": True,
                "minimum": None,
            },
        ]
    )
    manifest = generate_manifest(two_param_rule, "two params", client=FakeClient(manifest_text))
    assert {p.name for p in manifest.params} == {"first", "second"}


# --------------------------------------------------------------------------------------------
# Malformed manifests
# --------------------------------------------------------------------------------------------


def test_unknown_kind_is_rejected():
    bad_params = json.loads(_manifest_json())["params"]
    bad_params[0]["kind"] = "string"
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(
            ONE_PARAM_RULE, "max 2 hours", client=FakeClient(_manifest_json(params=bad_params))
        )
    assert "kind" in excinfo.value.reason
    assert "'string'" in excinfo.value.reason


def test_non_json_text_is_rejected():
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(ONE_PARAM_RULE, "max 2 hours", client=FakeClient("not json at all"))
    assert "not valid JSON" in excinfo.value.reason


def test_a_json_array_instead_of_an_object_is_rejected():
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(ONE_PARAM_RULE, "max 2 hours", client=FakeClient("[1, 2, 3]"))
    assert "JSON object" in excinfo.value.reason


def test_a_missing_label_is_rejected():
    payload = json.loads(_manifest_json())
    del payload["label"]
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(ONE_PARAM_RULE, "max 2 hours", client=FakeClient(json.dumps(payload)))
    assert "'label'" in excinfo.value.reason


def test_a_blank_description_is_rejected():
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(
            ONE_PARAM_RULE, "max 2 hours", client=FakeClient(_manifest_json(description="   "))
        )
    assert "'description'" in excinfo.value.reason


def test_params_that_is_not_a_list_is_rejected():
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(
            ONE_PARAM_RULE, "max 2 hours", client=FakeClient(_manifest_json(params="nope"))
        )
    assert "'params'" in excinfo.value.reason


def test_reads_history_that_is_not_a_boolean_is_rejected():
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(
            ONE_PARAM_RULE, "max 2 hours", client=FakeClient(_manifest_json(reads_history="yes"))
        )
    assert "'reads_history'" in excinfo.value.reason


def test_a_long_description_is_capped_rather_than_rejected():
    from generation.manifest import DESCRIPTION_MAX_CHARS

    manifest = generate_manifest(
        ONE_PARAM_RULE,
        "max 2 hours",
        client=FakeClient(_manifest_json(description="x" * (DESCRIPTION_MAX_CHARS + 500))),
    )
    assert len(manifest.description) == DESCRIPTION_MAX_CHARS


# --------------------------------------------------------------------------------------------
# Loading the class the manifest describes
# --------------------------------------------------------------------------------------------


def test_a_source_defining_no_rule_class_is_rejected():
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest("x = 1\n", "does nothing", client=FakeClient(_manifest_json()))
    assert "none" in excinfo.value.reason


def test_a_source_defining_two_rule_classes_is_rejected():
    two_classes = (
        "class First(BaseRule):\n"
        "    def evaluate(self, request, context):\n"
        "        return RuleResult.allow()\n"
        "\n"
        "class Second(BaseRule):\n"
        "    def evaluate(self, request, context):\n"
        "        return RuleResult.allow()\n"
    )
    with pytest.raises(ManifestRejectedError) as excinfo:
        generate_manifest(two_classes, "does nothing", client=FakeClient(_manifest_json(params=[])))
    assert "First" in excinfo.value.reason
    assert "Second" in excinfo.value.reason


def test_a_rule_importing_the_allowed_datetime_module_still_loads():
    """`_load_rule_class` must permit the same import allowlist a validated rule may use."""
    manifest = generate_manifest(ONE_PARAM_RULE, "max 2 hours", client=FakeClient(_manifest_json()))
    assert manifest.label


# --------------------------------------------------------------------------------------------
# Caller errors and backend failures
# --------------------------------------------------------------------------------------------


def test_a_blank_rule_source_is_a_caller_error_and_never_reaches_the_model():
    client = FakeClient(_manifest_json())
    with pytest.raises(ValueError):
        generate_manifest("   ", "max 2 hours", client=client)
    assert client.calls == []


def test_a_blank_description_argument_is_a_caller_error():
    client = FakeClient(_manifest_json())
    with pytest.raises(ValueError):
        generate_manifest(ONE_PARAM_RULE, "   ", client=client)
    assert client.calls == []


def test_a_backend_failure_surfaces_structurally():
    with pytest.raises(LLMCallError) as excinfo:
        generate_manifest(ONE_PARAM_RULE, "max 2 hours", client=ExplodingClient())
    assert excinfo.value.exit_code == 127


def test_manifest_rejection_and_call_failure_share_a_base():
    assert issubclass(ManifestRejectedError, GenerationError)
    assert issubclass(LLMCallError, GenerationError)
