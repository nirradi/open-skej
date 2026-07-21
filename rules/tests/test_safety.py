"""Tests for the AST safety validator.

The bulk of this file is a **bypass-attempt suite**: every blocked construct spelled at least three
different ways. One spelling per rule would only prove the validator rejects the example its author
had in mind, and the thing being defended against is precisely a model or an author reaching for a
second spelling. Each parametrised case is a source string that must be refused.

The other half matters just as much: an ordinary rule doing ordinary date arithmetic has to pass
cleanly, or the generation loop spends its retries fighting the validator instead of the problem.
"""

import ast

import pytest

from rules.safety import ALLOWED_IMPORTS, BLOCKED_NAMES, UnsafeRuleError, validate_source


def assert_rejected(src: str) -> str:
    """Assert ``src`` is refused, and return the message so a caller can check what it names."""
    with pytest.raises(UnsafeRuleError) as excinfo:
        validate_source(src)
    return str(excinfo.value)


# --------------------------------------------------------------------------------------------
# The legitimate case
# --------------------------------------------------------------------------------------------

# A rule as the generator is meant to emit one: parameterised, datetime-only, history-driven.
# ``BaseRule``, ``RuleResult`` and the context types are free names here rather than imports — they
# are supplied by the namespace the rule is loaded into, and `rules` is not an importable module as
# far as this validator is concerned.
LEGITIMATE_RULE = '''
from datetime import timedelta


class MaxBookingsPerWeekRule(BaseRule):
    """At most ``max_per_week`` bookings in the seven days before the request."""

    def __init__(self, max_per_week):
        self.max_per_week = max_per_week

    def evaluate(self, request, context):
        window_start = request.start_at - timedelta(days=7)
        recent = [b for b in context.history.bookings if b.start_at >= window_start]
        if len(recent) >= self.max_per_week:
            return RuleResult.deny(
                "You have already booked {} times this week.".format(self.max_per_week)
            )
        return RuleResult.allow()
'''


def test_legitimate_rule_passes_cleanly():
    assert validate_source(LEGITIMATE_RULE) is None


def test_allowed_imports_all_pass():
    for module in sorted(ALLOWED_IMPORTS):
        assert validate_source(f"import {module}") is None


@pytest.mark.parametrize(
    "src",
    [
        "from datetime import datetime, timedelta, timezone",
        "from zoneinfo import ZoneInfo",
        "import math",
        "import datetime as dt",
        "from datetime import timedelta as td",
    ],
    ids=["from-datetime", "from-zoneinfo", "import-math", "aliased", "aliased-member"],
)
def test_allowed_import_spellings_pass(src):
    assert validate_source(src) is None


def test_for_loops_are_allowed():
    # Only `while` is unbounded; iterating the capped history is the normal shape of a rule.
    src = """
def count(context):
    total = 0
    for booking in context.history.bookings:
        total = total + 1
    return total
"""
    assert validate_source(src) is None


# --------------------------------------------------------------------------------------------
# Syntax errors are refusals, not SyntaxErrors
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "def evaluate(self",
        "class Rule(BaseRule)\n    pass",
        "return = 1",
        "if True\n    pass",
    ],
    ids=["unclosed-paren", "missing-colon", "keyword-as-target", "no-colon-if"],
)
def test_syntax_error_raises_unsafe_rule_error(src):
    message = assert_rejected(src)
    assert "does not parse" in message


def test_unsafe_rule_error_is_not_a_syntax_error():
    # A caller handling "this candidate is unacceptable" catches one type. If UnsafeRuleError were a
    # SyntaxError subclass — or if a SyntaxError escaped — that guarantee would be a coin flip.
    assert not issubclass(UnsafeRuleError, SyntaxError)
    with pytest.raises(UnsafeRuleError):
        validate_source("def broken(")


def test_non_string_source_is_rejected():
    assert_rejected(ast.parse("x = 1"))  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------
# Bypass suite: imports
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "import os",
        "import sys",
        "import subprocess",
        "import os.path",
        "from os import path",
        "from subprocess import run",
        "import socket as datetime",
        "from os import system as timedelta",
        "from datetime import *",
        "from math import *",
        "from . import helpers",
        "from ..rules import interfaces",
        "import importlib",
        "import builtins",
    ],
    ids=[
        "os",
        "sys",
        "subprocess",
        "dotted",
        "from-os",
        "from-subprocess",
        "aliased-to-allowed-name",
        "member-aliased-to-allowed-name",
        "star-from-allowed",
        "star-from-math",
        "relative",
        "relative-parent",
        "importlib",
        "builtins",
    ],
)
def test_disallowed_imports_are_rejected(src):
    assert_rejected(src)


def test_aliasing_does_not_launder_a_blocked_module():
    # The alias is what the name *looks* like; the module is what it *is*.
    message = assert_rejected("import socket as datetime")
    assert "socket" in message


def test_import_inside_a_function_is_rejected():
    assert_rejected("def evaluate(self, request, context):\n    import os\n    return None")


# --------------------------------------------------------------------------------------------
# Bypass suite: dunder attribute access
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "x = ().__class__",
        "x = ().__class__.__base__.__subclasses__()",
        "x = evaluate.__globals__",
        "x = evaluate.__code__.co_consts",
        "x = request.__dict__",
        "x = type(request).__mro__",
        "x = ''.__class__.__mro__[1]",
        "x = [].__class__.__bases__",
        "def evaluate(self, request, context):\n    return self.__class__",
    ],
    ids=[
        "class-of-tuple",
        "subclasses-walk",
        "func-globals",
        "code-object",
        "instance-dict",
        "mro-via-type",
        "str-mro",
        "list-bases",
        "self-class-in-method",
    ],
)
def test_dunder_attribute_access_is_rejected(src):
    assert_rejected(src)


def test_dunder_names_are_rejected():
    for src in ("x = __builtins__", "x = __name__", "x = __file__"):
        assert_rejected(src)


# --------------------------------------------------------------------------------------------
# Bypass suite: blocked names
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(BLOCKED_NAMES))
def test_every_blocked_name_is_rejected_when_called(name):
    assert_rejected(f"x = {name}()")


@pytest.mark.parametrize(
    "src",
    [
        "x = eval('1 + 1')",
        "sneaky = eval",
        "x = [eval][0]('1')",
        "x = {'e': eval}['e']('1')",
        "def evaluate(self, request, context, helper=eval):\n    return helper('1')",
        "x = exec('y = 1')",
        "handlers = (exec, compile)",
        "x = compile('1', '<s>', 'eval')",
        "f = open('/etc/passwd')",
        "data = open",
        "mod = __import__('os')",
        "g = globals()",
        "scope = globals",
        "l = locals()",
        "v = vars()",
        "names = dir(request)",
        "attr = getattr(request, 'user_id')",
        "grab = getattr",
    ],
    ids=[
        "eval-call",
        "eval-rebound",
        "eval-via-list",
        "eval-via-dict",
        "eval-as-default-arg",
        "exec-call",
        "exec-in-tuple",
        "compile-call",
        "open-call",
        "open-rebound",
        "dunder-import",
        "globals-call",
        "globals-rebound",
        "locals-call",
        "vars-call",
        "dir-call",
        "getattr-call",
        "getattr-rebound",
    ],
)
def test_blocked_names_are_rejected_in_any_position(src):
    assert_rejected(src)


# --------------------------------------------------------------------------------------------
# Bypass suite: decorators
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "@staticmethod\ndef evaluate(request, context):\n    return None",
        "@property\ndef name(self):\n    return 'x'",
        "class Rule(BaseRule):\n    @property\n    def name(self):\n        return 'x'",
        "@dataclass\nclass Rule(BaseRule):\n    pass",
        "@a\n@b\ndef evaluate(self, request, context):\n    return None",
        "class Rule(BaseRule):\n    @classmethod\n    def build(cls):\n        return cls()",
        "@wrapper('arg')\ndef evaluate(self, request, context):\n    return None",
    ],
    ids=[
        "staticmethod",
        "property",
        "property-on-method",
        "dataclass-on-class",
        "stacked",
        "classmethod",
        "decorator-factory",
    ],
)
def test_decorators_are_rejected(src):
    assert_rejected(src)


# --------------------------------------------------------------------------------------------
# Bypass suite: global / nonlocal
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "def evaluate(self, request, context):\n    global cache\n    cache = 1",
        "def evaluate(self, request, context):\n    global a, b\n    a = b = 1",
        "def outer():\n    x = 1\n\n    def inner():\n        nonlocal x\n        x = 2",
        "def outer():\n    a = b = 1\n\n    def inner():\n        nonlocal a, b\n        a = 2",
    ],
    ids=["global-single", "global-multiple", "nonlocal-single", "nonlocal-multiple"],
)
def test_global_and_nonlocal_are_rejected(src):
    assert_rejected(src)


# --------------------------------------------------------------------------------------------
# Bypass suite: while loops
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "while True:\n    pass",
        "def evaluate(self, request, context):\n    while 1:\n        break",
        "def spin(n):\n    while n > 0:\n        n = n - 1",
        "def spin(n):\n    while n:\n        n = n - 1\n    else:\n        return n",
        "def spin(items):\n    for i in items:\n        while i:\n            i = None",
    ],
    ids=["while-true", "while-1", "while-condition", "while-else", "while-nested-in-for"],
)
def test_while_loops_are_rejected(src):
    assert_rejected(src)


# --------------------------------------------------------------------------------------------
# Bypass suite: comprehensions over names the source never binds
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "x = [c for c in mystery]",
        "x = {c for c in mystery}",
        "x = {c: c for c in mystery}",
        "x = (c for c in mystery)",
        "x = [helper(c) for c in (1, 2)]",
        "x = [c for c in (1, 2) if predicate(c)]",
        "x = [c for c in (1, 2) for d in mystery]",
        "def evaluate(self, request, context):\n    return [b for b in context.history if b in RC]",
    ],
    ids=[
        "listcomp-iterable",
        "setcomp-iterable",
        "dictcomp-iterable",
        "genexp-iterable",
        "unbound-call-in-element",
        "unbound-call-in-condition",
        "unbound-in-second-for",
        "unbound-in-method",
    ],
)
def test_comprehensions_over_unbound_names_are_rejected(src):
    assert_rejected(src)


@pytest.mark.parametrize(
    "src",
    [
        "def f(items):\n    return [i for i in items]",
        "def f(items):\n    return [len(i) for i in items]",
        "def f(items, floor):\n    return [i for i in items if i > floor]",
        "def f(items):\n    return {i: sorted(i) for i in items}",
        "LIMITS = (1, 2)\n\n\ndef f():\n    return [x for x in LIMITS]",
        "def f(items):\n    return sum(1 for i in items if i)",
    ],
    ids=[
        "argument",
        "safe-builtin",
        "two-arguments",
        "safe-builtin-in-dictcomp",
        "module-level-binding",
        "genexp-argument",
    ],
)
def test_comprehensions_over_bound_names_pass(src):
    assert validate_source(src) is None


def test_comprehension_message_names_the_offending_name():
    # The generation loop feeds this message back to the model, so it has to say what to fix.
    message = assert_rejected("x = [c for c in mystery]")
    assert "mystery" in message


# --------------------------------------------------------------------------------------------
# Messages carry a line number
# --------------------------------------------------------------------------------------------


def test_rejection_names_the_line():
    message = assert_rejected("x = 1\ny = 2\nimport os")
    assert "line 3" in message
