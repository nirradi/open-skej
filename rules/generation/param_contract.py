"""What a generated rule's constructor may assume about the values it is handed.

**The contract: every rule parameter arrives as a plain JSON number, and a duration arrives as an
integer count of minutes.** ``space_rules.params`` is a JSONB column, so nothing richer than a JSON
scalar can be stored in it; ``ParamKind`` has exactly two members and both are ``int`` underneath
(``rules/rules/registry.py``); and the write-boundary validator (``app.identity.service``) rejects
anything whose ``type()`` is not ``int``. So an ``int`` is not merely what usually arrives — it is
the only thing that *can* arrive. **Converting a unit-labelled parameter into the type the rule's
own logic wants is therefore the rule's job**, in ``__init__``, exactly as
``generation/stub.py``'s reference class and every hand-written build function in
``rules/rules/registry.py`` already do it.

This module is that contract, enforced. It reads the produced source and reports every place a
constructor parameter is used as if it were a ``timedelta``:

* compared against, or combined with, a ``timedelta(...)``, a ``.duration``, the difference of two
  of the contract's datetimes, or a **local holding any of those** — the last one is not a
  refinement but half the defect: a rule totalling time writes ``total = request.duration`` and
  compares the parameter against ``total``, which is the shape the second reported reproduction
  used;
* asked for ``.total_seconds()``, ``.days`` or ``.seconds``;
* given a ``timedelta(...)`` default.

Each of those is a ``TypeError`` or an ``AttributeError`` the first time the rule is built or run,
and the reason it is worth a dedicated check rather than being left to the tests is where the
failure lands: a rule whose ``__init__`` raises makes ``_build_canon`` unbuildable, which fails
closed for the **whole Space** — every resource, every date, every member — from the moment an
admin adds it (``ops/pending/bugs/generated-rule-duration-params-deny-every-booking.md``).

**Why here and not at the manifest call.** The manifest's ``_cross_check_params`` compares
parameter *names* and is the closest existing gate, but a rejection there is a job failure with no
retry, by design: the artifact already survived its adversarial suite and regenerating it to fix a
*description* would throw that away. A parameter-unit mistake is not a description mistake — it is
in the artifact itself, and it is precisely the kind of thing one correction turn fixes. So the
check runs inside ``generate_rule``, where a rejection is retryable and the finding is handed back
to the model verbatim, and again at load time in ``app.rule_catalog.hoist``, which is the only gate
a row written *before* this check existed still passes through.

It is a static, syntactic check over an AST — no execution, no model call — and it is deliberately
**conservative**: it classifies an expression as integer-valued only where it can see the
assignment that made it one, and reports nothing at all for source it cannot parse (the safety
validator already rejects that, and reporting a parse error twice in two vocabularies helps
nobody). A false positive here costs a retry the model did not need; the checks above are chosen so
that every one of them describes source that would genuinely raise.
"""

from __future__ import annotations

import ast

__all__ = [
    "CONTRACT_SUMMARY",
    "TIMEDELTA_ATTRS",
    "constructor_params",
    "param_contract_findings",
    "describe_param_contract_findings",
]


#: One sentence stating the contract, shared by the Generator's system prompt and the rejection
#: message fed back on a retry, so the instruction and the enforcement cannot drift apart.
CONTRACT_SUMMARY = (
    "every constructor argument arrives as a plain integer, and a duration arrives as an integer "
    "number of MINUTES — convert it to a timedelta inside __init__ "
    "(self.max_duration = timedelta(minutes=max_duration)) and compare against the converted value"
)

#: Attributes only a ``timedelta`` carries. Reading one off the ``int`` the wire actually delivers
#: raises ``AttributeError`` on the first evaluation.
TIMEDELTA_ATTRS = frozenset({"total_seconds", "days", "seconds", "microseconds"})

#: Attribute name that means "a span of time" on every engine type that has one —
#: ``request.duration`` and ``context.run.duration``. Both are ``timedelta``.
_DURATION_ATTR = "duration"

#: Every datetime-valued attribute the rule contract exposes (``rules/rules/interfaces.py``). The
#: difference of two of them is a ``timedelta``, which is how a rule spells "the gap between these
#: two bookings" — the shape the first reported reproduction of this defect actually used. Named as
#: a closed set rather than matched by a suffix pattern: the contract is fixed and short, and a
#: pattern would start classifying an attribute of the model's own invention.
_DATETIME_ATTRS = frozenset(
    {
        "start_at",
        "end_at",
        "now",
        "day_start",
        "day_end",
        "week_start",
        "week_end",
        "month_start",
        "month_end",
    }
)

_INT = "int"
_DURATION = "duration"


def constructor_params(rule_source: str) -> tuple[str, ...]:
    """The names ``rule_source``'s rule class takes in ``__init__``, in order, without ``self``.

    ``()`` for source that does not parse, defines no recognisable rule class, or defines one with
    no ``__init__`` — a zero-parameter rule and an unreadable one are not distinguished here,
    because every caller that cares about the difference has the source to say so itself. This is
    the same fact ``generation.manifest`` reads with ``inspect.signature``, taken from the AST
    instead so that no caller has to execute the source to learn it.
    """
    class_def = _rule_class(rule_source)
    if class_def is None:
        return ()
    init = _init_of(class_def)
    if init is None:
        return ()
    return tuple(_param_names(init))


def param_contract_findings(rule_source: str) -> tuple[str, ...]:
    """Every place ``rule_source`` treats a constructor parameter as a ``timedelta``.

    Empty when the source honours the contract — which includes source this module cannot read at
    all (see the module docstring). Each finding is one sentence naming the parameter, the line, and
    what to do about it, written to be handed straight back to the model on a retry.
    """
    class_def = _rule_class(rule_source)
    if class_def is None:
        return ()
    init = _init_of(class_def)
    if init is None:
        return ()

    params = set(_param_names(init))
    if not params:
        return ()

    findings: list[tuple[int, str]] = []
    findings.extend(_default_findings(init, params))

    # `self.x` aliases, resolved once over `__init__` and then treated as integer-valued
    # everywhere in the class. This is what catches the other half of the defect: a rule that
    # stores the raw parameter and only meets its type in `evaluate`, where
    # `request.duration > self.max_duration` raises inside the controller instead of at build time.
    int_attrs, alias_of = _integer_self_attrs(init, params)

    # Two walks rather than one, because a bare `Name` is only the parameter inside `__init__`;
    # anywhere else it is some other local that happens to share the name.
    findings.extend(_walk(init, params, int_attrs, alias_of))
    for node in class_def.body:
        if node is init:
            continue
        findings.extend(_walk(node, frozenset(), int_attrs, alias_of))

    seen: set[str] = set()
    ordered: list[str] = []
    for _, message in sorted(findings, key=lambda entry: entry[0]):
        if message not in seen:
            seen.add(message)
            ordered.append(message)
    return tuple(ordered)


def describe_param_contract_findings(findings: tuple[str, ...]) -> str:
    """The retry feedback for ``findings`` — the rejection reason handed back to the Generator.

    States the contract first and the findings second. The contract is the thing the model did not
    know; the findings are only where that showed. A message that led with the line numbers would
    invite a patch of those lines rather than the conversion the rule actually needs.
    """
    bullets = "\n".join(f"- {finding}" for finding in findings)
    return (
        "The rule's parameters do not match the values it will actually be handed: "
        f"{CONTRACT_SUMMARY}.\n{bullets}"
    )


# --- AST plumbing ------------------------------------------------------------------------------


def _rule_class(rule_source: str) -> ast.ClassDef | None:
    """The one rule class in ``rule_source``, or ``None`` if there is no single obvious one.

    Prefers a class naming ``BaseRule`` as a base — the shape the Generator's prompt requires and
    the hoister enforces — and falls back to the only class in the module. ``None`` for anything
    ambiguous: this check exists to name a specific parameter of a specific constructor, and it has
    nothing useful to say about source whose rule class it had to guess at.
    """
    try:
        tree = ast.parse(rule_source)
    except SyntaxError:
        return None

    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    subclasses = [
        node
        for node in classes
        if any(isinstance(base, ast.Name) and base.id == "BaseRule" for base in node.bases)
    ]
    if len(subclasses) == 1:
        return subclasses[0]
    if not subclasses and len(classes) == 1:
        return classes[0]
    return None


def _init_of(class_def: ast.ClassDef) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in class_def.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__":
            return node
    return None


def _param_names(init: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = init.args
    names = [arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    return [name for name in names if name != "self"]


def _default_findings(
    init: ast.FunctionDef | ast.AsyncFunctionDef, params: set[str]
) -> list[tuple[int, str]]:
    """A parameter whose default is a ``timedelta(...)`` is one the model believes it will be
    handed a ``timedelta`` for. The default is never used in practice — the schema declares the
    parameter and a form always submits it — so this is a statement of belief rather than a live
    failure, and it is reported for exactly that reason: it is the clearest evidence available that
    the constructor's other uses of the parameter mean the wrong type too.
    """
    args = init.args
    pairs: list[tuple[str, ast.expr | None]] = []
    positional = [*args.posonlyargs, *args.args]
    padding = len(positional) - len(args.defaults)
    for index, arg in enumerate(positional):
        pairs.append((arg.arg, args.defaults[index - padding] if index >= padding else None))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        pairs.append((arg.arg, default))

    findings = []
    for name, default in pairs:
        if name in params and default is not None and _is_timedelta_call(default):
            findings.append(
                (
                    getattr(default, "lineno", 0),
                    f"parameter {name!r} defaults to a timedelta (line "
                    f"{getattr(default, 'lineno', 0)}), but it is submitted as an integer number "
                    "of minutes — default it to a plain number and convert in the body",
                )
            )
    return findings


def _integer_self_attrs(
    init: ast.FunctionDef | ast.AsyncFunctionDef, params: set[str]
) -> tuple[frozenset[str], dict[str, str]]:
    """``self.X`` attributes ``__init__`` assigns a raw parameter to, and which parameter each came
    from.

    An attribute assigned more than once, or assigned anything other than a bare parameter name, is
    left out entirely rather than guessed at — ``self.x = timedelta(minutes=x)`` is the correct
    shape and must not be flagged, and a conditional pair of assignments is not something a
    syntactic pass should pretend to resolve.
    """
    assigned: dict[str, str | None] = {}
    for node in ast.walk(init):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        for target in targets:
            attr = _self_attr_name(target)
            if attr is None:
                continue
            source_param = value.id if isinstance(value, ast.Name) and value.id in params else None
            assigned[attr] = source_param if attr not in assigned else None

    alias_of = {attr: param for attr, param in assigned.items() if param is not None}
    return frozenset(alias_of), alias_of


def _self_attr_name(node: ast.expr) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


def _is_timedelta_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "timedelta"
    return isinstance(func, ast.Attribute) and func.attr == "timedelta"


def _duration_locals(
    node: ast.AST, params: frozenset[str] | set[str], int_attrs: frozenset[str]
) -> frozenset[str]:
    """Local names in ``node`` that hold a duration, so a comparison against one is still seen.

    Without this the check reads only the shape ``request.duration > self.max_duration``, and the
    second reproduction in the bug report is not that shape: a rule totalling time writes
    ``total = request.duration`` (or accumulates into ``total = timedelta()``) and compares the
    parameter against the local. That is the ordinary way to write "no more than N hours per week",
    and the ``TypeError`` it raises is the same one.

    Conservative in the same way ``_integer_self_attrs`` is. A name counts only where every plain
    assignment to it agrees that it is duration-valued; a name assigned two different kinds is
    dropped rather than guessed at. ``+=`` never establishes a name — it only disqualifies one, and
    only when an integer parameter is what is being added, because ``total += self.max_duration``
    means the author has already confused the two and the comparison downstream is not the place to
    say so.
    """
    kinds: dict[str, str | None] = {}
    augmented_with_int: set[str] = set()

    for child in ast.walk(node):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(child, ast.Assign):
            targets = list(child.targets)
            value = child.value
        elif isinstance(child, ast.AnnAssign) and child.value is not None:
            targets = [child.target]
            value = child.value
        elif isinstance(child, ast.AugAssign):
            if isinstance(child.target, ast.Name) and (
                _classify(child.value, params, int_attrs) == _INT
            ):
                augmented_with_int.add(child.target.id)
            continue
        else:
            continue

        kind = _classify(value, params, int_attrs)
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            kinds[target.id] = (
                kind if target.id not in kinds else (kind if kinds[target.id] == kind else None)
            )

    return frozenset(
        name for name, kind in kinds.items() if kind == _DURATION and name not in augmented_with_int
    )


def _classify(
    node: ast.expr,
    params: frozenset[str] | set[str],
    int_attrs: frozenset[str],
    duration_names: frozenset[str] = frozenset(),
) -> str | None:
    """``_INT``, ``_DURATION``, or ``None`` for an expression this pass will not judge.

    Deliberately shallow: it never descends into a ``timedelta(...)`` call, which is what keeps the
    correct shape ``request.duration > timedelta(minutes=self.max_minutes)`` clean — the integer
    parameter is an *argument to* the conversion there, not an operand of the comparison.
    """
    if isinstance(node, ast.Name):
        if node.id in params:
            return _INT
        if node.id in duration_names:
            return _DURATION
    if _is_timedelta_call(node):
        return _DURATION
    if isinstance(node, ast.Attribute):
        attr = _self_attr_name(node)
        if attr is not None:
            return _INT if attr in int_attrs else None
        if node.attr == _DURATION_ATTR:
            return _DURATION
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Sub)
        and _is_datetime(node.left)
        and _is_datetime(node.right)
    ):
        return _DURATION
    return None


def _is_datetime(node: ast.expr) -> bool:
    """One of the contract's datetime-valued attributes, read off something other than ``self``.

    ``self.<anything>`` is excluded because a rule's own attribute is whatever ``__init__`` put
    there, and this pass has already decided separately what that is.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _DATETIME_ATTRS
        and _self_attr_name(node) is None
    )


def _blame(node: ast.expr, params: frozenset[str] | set[str], alias_of: dict[str, str]) -> str:
    """The parameter name a flagged expression traces back to, for the finding's text."""
    if isinstance(node, ast.Name):
        return node.id
    attr = _self_attr_name(node)
    if attr is not None:
        return alias_of.get(attr, attr)
    return "the parameter"


def _walk(
    node: ast.AST,
    params: frozenset[str] | set[str],
    int_attrs: frozenset[str],
    alias_of: dict[str, str],
) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    # Resolved per call, because `_walk` is called once per method: a local named `total` in
    # `evaluate` says nothing about a local of that name in `__init__`.
    duration_names = _duration_locals(node, params, int_attrs)

    for child in ast.walk(node):
        if isinstance(child, ast.Compare):
            operands = [child.left, *child.comparators]
            pairs = list(zip(operands, operands[1:]))
        elif isinstance(child, ast.BinOp):
            pairs = [(child.left, child.right)]
        elif isinstance(child, ast.Attribute):
            if child.attr in TIMEDELTA_ATTRS:
                kind = _classify(child.value, params, int_attrs, duration_names)
                if kind == _INT:
                    name = _blame(child.value, params, alias_of)
                    findings.append(
                        (
                            child.lineno,
                            f"line {child.lineno} reads .{child.attr} off {name!r}, which is a "
                            "plain integer of minutes and has no timedelta attributes — convert "
                            "it in __init__ and read the converted value",
                        )
                    )
            continue
        else:
            continue

        for left, right in pairs:
            kinds = (
                _classify(left, params, int_attrs, duration_names),
                _classify(right, params, int_attrs, duration_names),
            )
            if _INT in kinds and _DURATION in kinds:
                offender = left if kinds[0] == _INT else right
                name = _blame(offender, params, alias_of)
                findings.append(
                    (
                        child.lineno,
                        f"line {child.lineno} uses {name!r} together with a timedelta, but "
                        f"{name!r} is a constructor parameter and arrives as a plain integer "
                        "number of minutes — that comparison raises TypeError; convert it in "
                        "__init__ with timedelta(minutes=...) and use the converted value here",
                    )
                )

    return findings
