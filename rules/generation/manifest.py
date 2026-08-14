"""Agent C: describe a verified rule for an admin picker, and declare its parameters.

``generate_manifest(rule_source, description, client=...)`` is one further model call, made
**after** the retry loop has already reported ``AttemptOutcome.PASSED``, against the exact source
that passed — never against a candidate still being iterated on. It answers two questions a
generated rule leaves open even once it is verified: what does an admin choosing between rule
types see (a label, a description), and what does a form built for it submit (a parameter schema)?

Unlike the Generator and the Tester, there is no retry turn here. ``ManifestRejectedError`` is a
job failure, not feedback fed back into another attempt — see that error's own docstring for why:
the rule already survived its adversarial suite, and regenerating it to fix a description would
throw away the one thing worth keeping.

**The cross-check is the load-bearing part of this module.** The manifest's declared parameter
names are checked against ``inspect.signature`` of the class the verified source actually defines,
not trusted from the model's own account of its constructor. A manifest that disagrees with
``__init__`` would let an admin form submit a body that raises ``TypeError`` building the rule —
which the adapter turns into a denial, which reads to that admin as every booking refused for no
visible reason. Loading the class to inspect it happens in a namespace deliberately mirroring
``app.rule_catalog``'s restricted one (imports limited to ``rules.safety.ALLOWED_IMPORTS``, builtins
limited to ``SAFE_BUILTINS``): this package cannot import the backend — the dependency runs the
other way, ``rules`` and ``generation`` are what the backend installs — so the same discipline is
reproduced here rather than shared, over source that has already passed ``validate_source`` once.
"""

from __future__ import annotations

import builtins
import inspect
import json
from dataclasses import dataclass
from typing import Any

from rules import BaseRule, ParamKind, RuleParam, interfaces
from rules.safety import ALLOWED_IMPORTS, SAFE_BUILTINS

from .errors import ManifestRejectedError
from .generator import strip_code_fence
from .harness import ENGINE_NAMES
from .llm import LLMClient

__all__ = [
    "generate_manifest",
    "build_manifest_prompt",
    "RuleManifest",
    "SYSTEM_PROMPT",
    "MANIFEST_SYSTEM_PROMPT",
    "DESCRIPTION_MAX_CHARS",
]

#: A rule description is a sentence or two for a picker, not a paragraph — the same order of
#: magnitude as `_RULE_PROMPT_MAX` on the wire boundary that accepts the constraint it describes.
DESCRIPTION_MAX_CHARS = 500


SYSTEM_PROMPT = """\
You are given a booking rule that has already been generated, validated, and verified against \
its own adversarial test suite — plus the plain-English constraint it was written to enforce. \
Your job is not to judge the rule. Your job is to describe it for an admin choosing which rule \
to add to their venue, and to declare its parameters so a form can be built for it.

Return ONLY a JSON object. No explanation, no commentary, no markdown fence.

## Shape

{
  "label": "Maximum hours per day",
  "description": "Caps the total booked time one member may hold on any single day.",
  "params": [
    {"name": "max_minutes", "kind": "integer", "label": "Daily limit", "unit": "minutes",
     "required": true, "minimum": 1}
  ],
  "reads_history": true
}

## label

A short name for the picker an admin sees alongside rule types like "Maximum duration" and "Max \
bookings per week". A few words, not a sentence.

## description

One or two sentences, written for an admin who will never read the Python: what the rule refuses, \
in plain terms. Never the constraint verbatim and never a restatement of the code — say what an \
admin gains by adding it. Never name a clock time, a calendar date, or a timezone: you cannot know \
which venue this rule will be added to, and any time value you had would be UTC wearing no label \
— the same reason the rule's own denial copy is forbidden from naming one.

## params

One entry per argument `__init__` actually takes, in the order it takes them. This is the part \
that must be exact: the name here becomes the JSON key an admin form submits, and it is checked \
against the constructor's real signature — a name that does not match exactly makes every \
submission of this rule fail before the rule is ever asked to evaluate anything. Read the \
`__init__` you were given, not the constraint text: an argument named `max_bookings` is declared \
here as "max_bookings", never as "limit" or "count" because that reads better.

`kind` is one of exactly two values and nothing else: `"integer"` for a plain count (a number of \
days, minutes, or bookings), or `"local_time"` for a wall-clock time of day. A form exists for \
only these two; naming a third kind throws the whole manifest away.

`unit` is a short word for an integer parameter's unit ("minutes", "days", "bookings"), or `null` \
for a `local_time` parameter, which has none. `required` is `true` unless the constructor gives \
the argument a default value. `minimum` is the smallest legal value for an integer parameter, or \
`null` if there is none.

An `__init__` that takes no arguments beyond `self` gets an empty `params` list.

## reads_history

`true` if `evaluate` reads `context.history.bookings` anywhere in the source you were given, \
`false` otherwise.\
"""

#: The same prompt under a name that survives being re-exported next to the Generator's and the
#: Tester's — both of those modules call theirs `SYSTEM_PROMPT` too, which reads correctly in each
#: and would collide in the package.
MANIFEST_SYSTEM_PROMPT = SYSTEM_PROMPT


@dataclass(frozen=True)
class RuleManifest:
    """What Agent C decided about one verified candidate.

    ``params`` are ``rules.RuleParam`` — the exact schema ``rules.RuleType`` and the admin form
    already speak, so nothing downstream of this needs a second shape to translate through.
    ``reads_history`` is not simply what the model claimed; see ``generate_manifest`` for the
    correction made against the source itself.
    """

    label: str
    description: str
    params: tuple[RuleParam, ...]
    reads_history: bool


def generate_manifest(
    rule_source: str,
    description: str,
    *,
    client: LLMClient,
    model: str | None = None,
) -> RuleManifest:
    """Describe verified ``rule_source`` — generated for the constraint ``description`` — as a
    :class:`RuleManifest`.

    Raises ``LLMCallError`` if the backend could not produce a completion, and
    ``ManifestRejectedError`` if what it produced is not an acceptable manifest: invalid JSON, a
    missing field, an unknown ``kind``, or a parameter list that disagrees with what ``rule_source``
    actually constructs. Both are failures the caller does not retry (see the module docstring).

    ``client`` is required and has no default, for the same reason it is on ``generate_rule`` and
    ``generate_tests``: a module-level default is a way to call a model by accident.
    """
    if not rule_source or not rule_source.strip():
        raise ValueError("rule_source must be non-empty rule source to describe")
    if not description or not description.strip():
        raise ValueError("description must be a non-empty rule description")

    response = client.complete(
        system=SYSTEM_PROMPT,
        prompt=build_manifest_prompt(rule_source, description),
        model=model,
    )
    text = strip_code_fence(response.text)

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ManifestRejectedError(
            f"The model's manifest is not valid JSON: {exc}", payload=text
        ) from exc
    if not isinstance(payload, dict):
        raise ManifestRejectedError("The model's manifest is not a JSON object.", payload=text)

    label = _string_field(payload, "label", payload=text)
    manifest_description = _string_field(payload, "description", payload=text)[
        :DESCRIPTION_MAX_CHARS
    ]

    raw_params = payload.get("params")
    if not isinstance(raw_params, list):
        raise ManifestRejectedError("The manifest's 'params' must be a list.", payload=text)
    params = tuple(_build_param(entry, payload=text) for entry in raw_params)

    declared_reads_history = payload.get("reads_history")
    if not isinstance(declared_reads_history, bool):
        raise ManifestRejectedError(
            "The manifest's 'reads_history' must be a boolean.", payload=text
        )

    rule_class = _load_rule_class(rule_source)
    _cross_check_params(params, rule_class, payload=text)

    return RuleManifest(
        label=label,
        description=manifest_description,
        params=params,
        # The damaging direction is a false negative: it would skip the Space-wide history query
        # and run a rule that needs history against none at all, which is silently permissive —
        # a "no more than three a week" rule counting zero bookings it was never given and
        # allowing one it should have refused. A false positive only costs one query a rule then
        # ignores. So the source can elevate a `false` claim to `true` (a rule reading
        # `context.run` genuinely needs history even when its own account of itself says
        # otherwise — see `_mentions_history`), but it can never suppress a `true` claim: the
        # model's own `true` is trusted outright, because downgrading it on the strength of a
        # substring miss would reintroduce the exact false negative this whole correction exists
        # to close. When the two disagree, whichever one says `true` wins.
        reads_history=declared_reads_history or _mentions_history(rule_source),
    )


def build_manifest_prompt(rule_source: str, description: str) -> str:
    """The user turn: the constraint, and the verified rule written for it.

    Mirrors ``tester.build_test_prompt``'s shape for the same reason: everything durable is in the
    system prompt, so the part that varies per call stays this short and both are delimited the
    same way.
    """
    return (
        "This rule was generated for the following booking constraint and has already passed its "
        "own adversarial test suite:\n\n"
        f"<constraint>\n{description.strip()}\n</constraint>\n\n"
        f"<rule>\n{rule_source.strip()}\n</rule>\n\n"
        "Describe it and declare its parameters. Return only the JSON object."
    )


def _string_field(fields: dict[str, Any], name: str, *, payload: str) -> str:
    """A required non-empty string field, or ``ManifestRejectedError`` naming the missing one."""
    value = fields.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ManifestRejectedError(
            f"The manifest is missing a non-empty {name!r} field.", payload=payload
        )
    return value.strip()


def _mentions_history(source: str) -> bool:
    """Whether ``source`` looks like it reads booking history at all — directly via
    ``context.history``, or indirectly via ``context.run``, which the adapter resolves from history
    before the rule ever runs (``RunContext``, ``.claude/rules/rule-engine.md``, "It resolves the
    run"). A rule naming only ``context.run`` never mentions "history" in its own source, so
    checking for that word alone under-reports exactly the rules task 8.8 exists to cover — see
    ``rules/rules/registry.py``'s module docstring on what ``reads_history`` means for
    ``max_consecutive_duration``, the hand-written rule in the identical shape.

    A substring check, not an AST walk, and deliberately over-inclusive: a false positive costs one
    history query a rule then ignores, while a false negative hands a counting rule an empty
    history and makes it silently permissive — the one direction this codebase never accepts. When
    the cheap check is wrong in the safe direction, take the cheap check.
    """
    return "history" in source or "context.run" in source


def _build_param(entry: Any, *, payload: str) -> RuleParam:
    if not isinstance(entry, dict):
        raise ManifestRejectedError(
            "Each entry in 'params' must be a JSON object.", payload=payload
        )

    kind_raw = entry.get("kind")
    try:
        kind = ParamKind(kind_raw)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in ParamKind)
        raise ManifestRejectedError(
            f"Unknown param kind {kind_raw!r}; expected one of {allowed}.", payload=payload
        ) from exc

    try:
        return RuleParam(
            name=entry.get("name"),
            kind=kind,
            label=entry.get("label"),
            unit=entry.get("unit"),
            required=bool(entry.get("required", True)),
            minimum=entry.get("minimum"),
        )
    except (TypeError, ValueError) as exc:
        raise ManifestRejectedError(
            f"Malformed param entry {entry!r}: {exc}", payload=payload
        ) from exc


def _cross_check_params(params: tuple[RuleParam, ...], rule_class: type, *, payload: str) -> None:
    """Raise unless ``params`` names exactly the arguments ``rule_class.__init__`` takes.

    Same set, no extras, no omissions apart from ``self`` — this is the check the module docstring
    calls load-bearing: a mismatch here is a form whose own submission raises ``TypeError``.
    """
    manifest_names = {param.name for param in params}
    signature = inspect.signature(rule_class.__init__)
    constructor_names = {
        name
        for name, parameter in signature.parameters.items()
        if name != "self"
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }

    if manifest_names == constructor_names:
        return

    missing = sorted(constructor_names - manifest_names)
    extra = sorted(manifest_names - constructor_names)
    detail = "; ".join(
        part
        for part in (
            f"missing {missing}" if missing else "",
            f"extra {extra}" if extra else "",
        )
        if part
    )
    raise ManifestRejectedError(
        f"The manifest's params do not match {rule_class.__name__}.__init__: {detail}.",
        payload=payload,
    )


def _load_rule_class(rule_source: str) -> type[BaseRule]:
    """Exec verified ``rule_source`` in a restricted namespace and return the one ``BaseRule``
    subclass it defines.

    Mirrors ``app.rule_catalog._restricted_namespace`` / ``.hoist`` — see the module docstring for
    why this is a second copy rather than a shared one. Unlike that path there is no bytecode to
    trust instead of source: ``rule_source`` is the exact string the retry loop already ran through
    ``validate_source`` and the sandbox, so this exec is over source, not a marshalled blob.
    """
    namespace = _restricted_namespace()
    try:
        exec(compile(rule_source, "<manifest>", "exec"), namespace)  # noqa: S102 - see docstring
    except Exception as exc:
        raise ManifestRejectedError(
            f"The verified rule source could not be executed while building its manifest: {exc!r}",
            payload=rule_source,
        ) from exc

    classes = [
        obj
        for obj in namespace.values()
        if isinstance(obj, type) and issubclass(obj, BaseRule) and obj is not BaseRule
    ]
    if len(classes) != 1:
        found = ", ".join(sorted(cls.__name__ for cls in classes)) or "none"
        raise ManifestRejectedError(
            f"Expected exactly one BaseRule subclass to build a manifest from, found "
            f"{len(classes)}: {found}.",
            payload=rule_source,
        )
    return classes[0]


def _guarded_import(
    name: str,
    globals: Any = None,
    locals: Any = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    """`__import__` restricted to `rules.safety.ALLOWED_IMPORTS` — see `_load_rule_class`."""
    root = name.split(".")[0]
    if level != 0 or root not in ALLOWED_IMPORTS:
        raise ImportError(
            f"a generated rule may import only {', '.join(sorted(ALLOWED_IMPORTS))}; "
            f"{name!r} is not one of them"
        )
    return builtins.__import__(name, globals, locals, fromlist, level)


def _restricted_namespace() -> dict[str, Any]:
    """The globals `_load_rule_class` execs verified source in — never the real builtins.

    See `app.rule_catalog._restricted_namespace`, which this deliberately mirrors: `SAFE_BUILTINS`
    plus the two pieces of execution machinery a class statement and an import need
    (`__build_class__`, a guarded `__import__`), plus the seven engine free names every verified
    rule is written against.
    """
    namespace: dict[str, Any] = {
        "__name__": "generation.manifest.<verified>",
        "__builtins__": {
            **{name: getattr(builtins, name) for name in sorted(SAFE_BUILTINS)},
            "__build_class__": builtins.__build_class__,
            "__import__": _guarded_import,
        },
    }
    namespace.update({name: getattr(interfaces, name) for name in ENGINE_NAMES})
    return namespace
