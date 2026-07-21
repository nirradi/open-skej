"""Static safety validation for candidate rule source, run before anything executes it.

``validate_source`` parses source into an AST and rejects constructs a booking rule has no business
using. It is the *authoring-time* half of safe execution: it refuses the obvious before the source
is written anywhere or handed to a test runner. The subprocess sandbox is the other half, and
neither is sufficient alone — this pass cannot bound CPU or memory, and the sandbox cannot tell a
rule that reads the filesystem from one that reads a booking.

**Fail closed.** Anything the validator cannot positively establish as safe is unsafe. That includes
source it cannot parse: a syntax error raises ``UnsafeRuleError``, never ``SyntaxError``, so a
caller that handles "this candidate is not acceptable" cannot let an unparseable one through by
catching only the one exception type it expected.

What is rejected, and why each one:

* **Imports outside** ``datetime``, ``zoneinfo``, ``math``. A rule does date arithmetic on the
  context it was handed; every other module is a capability it was not meant to have. Star imports
  are rejected outright regardless of the module, since they bind names the validator cannot see.
* **Attribute access beginning with** ``__``. ``().__class__.__base__.__subclasses__()`` reaches the
  entire loaded object graph from a literal, and no allowlist of module names constrains it. Barring
  the whole prefix also catches name-mangled privates — collateral, and cheap for a rule to avoid.
* **The names in** ``BLOCKED_NAMES``. Each is a way to obtain or execute code the validator never
  saw. ``vars`` and ``dir`` are on the list because ``vars()`` with no argument *is* ``locals()``;
  blocking one spelling and not the other would be theatre.
* **Decorators.** A decorator replaces the object it is applied to with the result of an arbitrary
  call, so what a validated ``evaluate`` actually is at call time would no longer be what was read
  here. Rules need none.
* **``global`` / ``nonlocal``.** Both let a rule reach out of its own frame and mutate state shared
  with whatever runs it next. A rule is a pure verdict on one request.
* **``while`` loops.** Unbounded by construction. ``for`` over the context's own history is bounded
  by a history window the engine already caps.
* **Comprehensions over names bound nowhere in the source** — see ``_check_comprehension``.

The check is deliberately syntactic. It makes no attempt to decide what a name will hold at runtime,
because that question has no static answer; it decides only whether the *spelling* is one the rule
canon permits.
"""

from __future__ import annotations

import ast

__all__ = [
    "validate_source",
    "UnsafeRuleError",
    "ALLOWED_IMPORTS",
    "BLOCKED_NAMES",
    "SAFE_BUILTINS",
]

#: The only modules a rule may import. Date arithmetic and nothing else.
ALLOWED_IMPORTS = frozenset({"datetime", "zoneinfo", "math"})

#: Names a rule may never mention, in any position. Every one of them either executes source the
#: validator never inspected or hands back a namespace from which anything can be reached.
BLOCKED_NAMES = frozenset(
    {
        "exec",
        "eval",
        "compile",
        "open",
        "__import__",
        "getattr",
        "globals",
        "locals",
        "vars",
        "dir",
    }
)

#: Builtins a comprehension may use despite being bound nowhere in the source. All are pure and
#: return data, not namespaces or callables reachable from them.
SAFE_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "divmod",
        "enumerate",
        "float",
        "frozenset",
        "int",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)

_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


class UnsafeRuleError(Exception):
    """Candidate rule source is not safe to execute, and no attempt was made to run it.

    Raised for every rejection reason including an unparseable source, so a caller has exactly one
    exception type to handle. The message names the construct and its line, because the generation
    loop feeds it back to the model as the reason to try again.
    """


def validate_source(src: str) -> None:
    """Validate rule source. Returns ``None`` if it is safe, raises ``UnsafeRuleError`` if not.

    Returning nothing is deliberate: there is no "safe enough" verdict to inspect and no boolean to
    forget to check. Either the call returns and the source passed, or it raised.
    """
    if not isinstance(src, str):
        raise UnsafeRuleError(f"Rule source must be a str, got {type(src).__name__}")

    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        # Deliberately not chained into a re-raised SyntaxError: a caller catching UnsafeRuleError
        # must not have to also catch SyntaxError to be sure nothing unvalidated gets through.
        raise UnsafeRuleError(f"Rule source does not parse: {exc.msg} (line {exc.lineno})") from exc

    _Validator(_bound_names(tree)).visit(tree)


def _bound_names(tree: ast.AST) -> frozenset[str]:
    """Every name the source itself binds, anywhere in it.

    Flat and non-lexical on purpose. This set answers "does the source define this name at all?",
    which is the question the comprehension check needs; it is not a scope model and is not trying
    to be one. A comprehension referring to a name bound only in some other function is a
    ``NameError`` waiting to happen, not an escape, and diagnosing that is not this module's job.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.alias):
            bound.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return frozenset(bound)


class _Validator(ast.NodeVisitor):
    """Walks the tree and raises on the first construct that is not permitted."""

    def __init__(self, bound: frozenset[str]) -> None:
        self._bound = bound

    # -- imports ---------------------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in ALLOWED_IMPORTS:
                _fail(node, f"import of {alias.name!r} is not allowed", _import_hint())
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            # A relative import resolves against whatever package the source is dropped into, so
            # what it reaches is decided by the caller's layout rather than by this allowlist.
            _fail(node, "relative imports are not allowed", _import_hint())
        module = node.module or ""
        root = module.split(".")[0]
        if root not in ALLOWED_IMPORTS:
            _fail(node, f"import from {module!r} is not allowed", _import_hint())
        for alias in node.names:
            if alias.name == "*":
                _fail(
                    node,
                    f"star import from {module!r} is not allowed",
                    "it binds names this validator cannot see",
                )
        self.generic_visit(node)

    # -- names and attributes --------------------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BLOCKED_NAMES:
            _fail(node, f"the name {node.id!r} is not allowed")
        if node.id.startswith("__"):
            _fail(node, f"the dunder name {node.id!r} is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            _fail(
                node,
                f"attribute access to {node.attr!r} is not allowed",
                "dunder attributes reach the object graph behind any value",
            )
        self.generic_visit(node)

    # -- statements ------------------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_decorators(node, "function")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_decorators(node, "function")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check_decorators(node, "class")
        self.generic_visit(node)

    def _check_decorators(self, node: ast.AST, kind: str) -> None:
        decorators = getattr(node, "decorator_list", [])
        if decorators:
            _fail(
                decorators[0],
                f"decorators on a {kind} are not allowed",
                "a decorator replaces what was validated with the result of an arbitrary call",
            )

    def visit_Global(self, node: ast.Global) -> None:
        _fail(node, "'global' is not allowed", "a rule may not reach outside its own frame")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        _fail(node, "'nonlocal' is not allowed", "a rule may not reach outside its own frame")

    def visit_While(self, node: ast.While) -> None:
        _fail(
            node,
            "'while' loops are not allowed",
            "they are unbounded; iterate the context's own history instead",
        )

    # -- comprehensions --------------------------------------------------------------------

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._check_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._check_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._check_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._check_comprehension(node)

    def _check_comprehension(self, node: ast.AST) -> None:
        """Reject a comprehension reading any name the source never binds.

        A comprehension runs in an implicit scope of its own, which makes it the one place where
        "which name is this?" stops being answerable by reading the enclosing lines. A name bound
        nowhere in the source is therefore something supplied by whatever executes it — a builtin,
        or a global injected by the runner — and reaching for one is how a comprehension becomes a
        foothold rather than a loop. The pure builtins in ``SAFE_BUILTINS`` are exempt: they return
        data, not a route to anything.
        """
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                if inner.id not in self._bound and inner.id not in SAFE_BUILTINS:
                    _fail(
                        inner,
                        f"comprehension reads {inner.id!r}, which this source never binds",
                        "a comprehension may only use names the rule itself defines",
                    )
        self.generic_visit(node)


def _import_hint() -> str:
    return "permitted modules are " + ", ".join(sorted(ALLOWED_IMPORTS))


def _fail(node: ast.AST, what: str, why: str | None = None) -> None:
    """Raise ``UnsafeRuleError`` naming the construct and where it is."""
    line = getattr(node, "lineno", None)
    where = f" (line {line})" if line is not None else ""
    detail = f": {why}" if why else ""
    raise UnsafeRuleError(f"Unsafe rule source{where}: {what}{detail}")
