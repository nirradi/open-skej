"""Agent B: the adversary. Given a candidate rule, write the tests that try to break it.

``generate_tests(rule_source, description, client=...)`` returns a pytest module that imports the
candidate and asserts on it. It is a *separate* call to the model from the one that wrote the rule,
given the rule's source and the original English constraint, and told its job is to find the case
the rule gets wrong. A generator asked to write its own tests writes tests that pass.

The output is only ever executed in the sandbox, against a candidate that is not part of the
application. Nothing here is imported by anything.

**Why test source is not put through ``validate_source``.** The safety validator encodes what a
*rule* may do, and it is strict because a rule is code the booking API will eventually run
in-process on every request: no imports beyond ``datetime``, ``zoneinfo`` and ``math``, no
decorators, no dunder attributes. Test code legitimately needs constructs a rule must never have —
``import pytest`` first among them, ``@pytest.mark.parametrize`` next, and ``pytest.raises`` to
assert that a rule raises rather than passing. Running the rule validator over it would reject every
useful suite on its first line, and the only way to satisfy both would be to weaken the validator
for rules too.

So the boundary for test code is the *sandbox*, not the validator: a wall-clock timeout, a memory
cap, no inherited environment, and a temp directory that is deleted with the run. That is the half
of safe execution built for code whose shape cannot be predicted, and it is the half that applies
here. The two checks this module does make — that the source parses, and that it defines at least
one test — are usability checks, not safety ones: they catch a model that replied with prose before
a sandbox run is spent finding out.
"""

from __future__ import annotations

import ast

from .errors import SuiteRejectedError
from .generator import strip_code_fence
from .harness import ENGINE_MODULE_NAME
from .llm import DEFAULT_MODEL, LLMClient

__all__ = [
    "generate_tests",
    "build_test_prompt",
    "SYSTEM_PROMPT",
    "TESTER_SYSTEM_PROMPT",
]


_ENGINE_MODULE = ENGINE_MODULE_NAME.removesuffix(".py")


SYSTEM_PROMPT = f"""\
You are the adversary. You are given a booking rule someone else wrote and the plain-English \
constraint it was supposed to enforce, and you write the pytest module that finds out whether it \
actually does. Your job is not to confirm the rule works. Your job is to find the input it gets \
wrong.

Return ONLY Python source for one pytest module. No explanation, no commentary, no markdown fence.

## How the module is laid out

The rule is in a module called `candidate_rule`, next to your file. The engine types are in a \
module called `{_ENGINE_MODULE}`. Import what you need:

    from candidate_rule import TheRuleClass
    from {_ENGINE_MODULE} import (
        BaseRule, BookingRecord, BookingRequest, CalendarContext,
        Context, HistoryContext, RuleResult, UserContext, Weekday,
    )

Import the rule class by the exact name it is defined with in the source you were given. Construct \
it with the parameters its `__init__` takes. Do not rewrite, patch, subclass or monkeypatch the \
rule — you are testing the code as written, and a test that repairs it in passing tests nothing.

## The types

    BookingRequest(user_id, resource_id, start_at, end_at)   .duration is end_at - start_at
    BookingRecord(user_id, resource_id, start_at, end_at)    no status field of any kind
    UserContext(user_id)
    CalendarContext(week_starts_on=Weekday.MONDAY, now=<datetime>)
    HistoryContext(bookings=(...))
    Context(user=..., calendar=..., history=...)
    RuleResult has .passed (bool) and .fail_reason (str or None)

Every datetime must be timezone-aware UTC with a zero offset — `datetime(2026, 3, 2, 9, 0, \
tzinfo=timezone.utc)`. A naive datetime or a non-zero offset raises at construction, so a test \
that uses one fails for a reason that has nothing to do with the rule.

`Context` also enforces that every booking in the history is within one calendar month or a \
rolling week of `now`, whichever is wider. Anchor `now` near the bookings you are writing about, \
or the Context itself raises before the rule is ever called.

## What the module must contain

Use fixed literal datetimes everywhere. NEVER call `datetime.now()`, `date.today()` or anything \
else that reads the clock: a test whose verdict depends on the day it runs is a test that will \
fail in a month for no reason.

1. POSITIVE CASES. At least two bookings that plainly satisfy the constraint, asserted to pass. \
Include a boring one from the middle of the allowed range, not only edge cases.

2. THE EXACT BOUNDARY, BOTH SIDES. If the rule allows n of something, assert the nth passes AND \
the (n+1)th is refused. If it bounds a duration, assert the exact limit passes and one second over \
is refused. Bounds are the whole reason this rule exists and off-by-one is how it will be wrong.

3. WINDOW EDGES, TO THE INSTANT. Where the rule counts bookings in a week or a month, pin the \
boundary: a booking one microsecond before the window opens must not count toward it, and one \
exactly on the boundary must count on the side it starts. Windows are half-open `[start, end)`. \
Cross a month boundary and a year rollover where the rule is monthly. Include a booking that \
straddles a boundary and assert which side it lands on.

4. A FAIL-CLOSED PROBE. Feed the rule input it cannot meaningfully evaluate, and assert it does \
not answer "allowed". Denying is correct, and raising is correct — the engine's controller catches \
an exception and converts it to a refusal. The one unacceptable answer is a pass, because a rule \
that swallows its own confusion into an allow looks exactly like a working rule that never denies, \
and is discovered by two people standing on the same court. Write it like this:

    def test_fails_closed_on_input_it_cannot_evaluate():
        rule = TheRuleClass(...)
        try:
            result = rule.evaluate(unusable_request, unusable_context)
        except Exception:
            return                      # raising is fail-closed; the controller contains it
        assert not result.passed, "a rule that cannot decide must not allow the booking"

   Choose the unusable input from what would actually confuse THIS rule: a history holding a \
different user's bookings, a context whose `now` sits far from the request, an empty history where \
the rule counts, a request whose resource the rule has no record of.

5. RETURN-TYPE CHECKS. Assert that `evaluate` returns a `RuleResult`, and that a refusal carries a \
non-empty `fail_reason`. A rule returning `True`, `None` or a bare string is a real failure mode \
and the engine treats it as a refusal.

## Style

Plain `def test_*()` functions and plain `assert`. `import pytest` if you want `pytest.raises` or \
`parametrize`; you do not need a conftest, a fixture file, or a class. Give each test a name that \
says which case it pins. Put a short assertion message on the ones where a bare `assert` would not \
say what broke.

Test only what the constraint you were given actually says. Do not invent a second constraint and \
assert the rule enforces that too — a rule is not wrong for failing to implement something nobody \
asked it for.\
"""


#: The same prompt under a name that survives being re-exported next to the Generator's. Both
#: modules call theirs ``SYSTEM_PROMPT``, which reads correctly in each and collides in the package.
TESTER_SYSTEM_PROMPT = SYSTEM_PROMPT


def generate_tests(
    rule_source: str,
    description: str,
    *,
    client: LLMClient,
    model: str = DEFAULT_MODEL,
) -> str:
    """Generate a pytest module exercising ``rule_source``, for the constraint ``description``.

    Raises ``LLMCallError`` if the backend could not produce a completion and
    ``SuiteRejectedError`` if what it produced is not a usable test module. Only the second is worth
    another attempt.

    ``client`` is required and has no default, for the same reason it is in ``generate_rule``: a
    module-level default is a way to call a model by accident, including from a test that meant to
    mock one.
    """
    if not rule_source or not rule_source.strip():
        raise ValueError("rule_source must be non-empty rule source to write tests against")
    if not description or not description.strip():
        raise ValueError("description must be a non-empty rule description")

    response = client.complete(
        system=SYSTEM_PROMPT,
        prompt=build_test_prompt(rule_source, description),
        model=model,
    )
    source = strip_code_fence(response.text)

    if not source.strip():
        raise SuiteRejectedError("The model returned no Python source at all.", source=source)

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SuiteRejectedError(
            f"The test module does not parse: {exc.msg} (line {exc.lineno})",
            source=source,
        ) from exc

    if not _has_test_function(tree):
        # pytest exits 5 on this and the sandbox reports it as a crash, correctly — but "no tests
        # were collected" read off an exit code is a much worse thing to hand back to a model than
        # a sentence saying what was missing.
        raise SuiteRejectedError(
            "The test module defines no test function; pytest would collect nothing and the "
            "candidate would stay unverified.",
            source=source,
        )

    return source


def build_test_prompt(rule_source: str, description: str) -> str:
    """The user turn: the constraint, and the candidate that claims to enforce it.

    Both are delimited, and the constraint is labelled as the thing the rule is *supposed* to do
    rather than the thing it does. The distinction is the Tester's entire job: given only the
    source, a model tends to write tests that describe the code's behaviour back to it, which
    passes whatever the code happens to do.
    """
    return (
        "This rule is supposed to enforce the following booking constraint:\n\n"
        f"<constraint>\n{description.strip()}\n</constraint>\n\n"
        "This is the rule that was written for it:\n\n"
        f"<rule>\n{rule_source.strip()}\n</rule>\n\n"
        "Write the pytest module that finds out whether it really does. Return only the Python "
        "source."
    )


def _has_test_function(tree: ast.Module) -> bool:
    """Whether anything in ``tree`` is something pytest would collect as a test."""
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")
        for node in ast.walk(tree)
    )
