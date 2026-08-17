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
from .llm import LLMClient

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

    from datetime import datetime, timedelta, timezone
    from candidate_rule import TheRuleClass
    from {_ENGINE_MODULE} import (
        BaseRule, BookingRecord, BookingRequest, CalendarContext,
        Context, HistoryContext, LocalFrame, RuleResult, RunContext, UserContext, Weekday,
    )

NOTHING IS A FREE NAME IN YOUR MODULE. Unlike the rule you are testing, your file is loaded as \
an ordinary module and no namespace binds anything for you: every name you use, you import. \
`datetime` and `timezone` are the ones this costs a whole run when they are missing, because \
every test writes `datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)` and a missing import fails \
every one of them with `NameError` before the rule is ever called — reported as the rule being \
wrong, which it is not.

Import the rule class by the exact name it is defined with in the source you were given. Construct \
it with the parameters its `__init__` takes. Do not rewrite, patch, subclass or monkeypatch the \
rule — you are testing the code as written, and a test that repairs it in passing tests nothing.

## The types

    BookingRequest(user_id, resource_id, start_at, end_at)   .duration is end_at - start_at
    BookingRecord(user_id, resource_id, start_at, end_at)    no status field of any kind
    UserContext(user_id)
    CalendarContext(week_starts_on, now)                     a Weekday and a datetime
    HistoryContext(bookings)                                 a tuple of BookingRecord
    LocalFrame(day_start, day_end, week_start, week_end, month_start, month_end,
               weekday, start_minutes, end_minutes)
    RunContext(start_at, end_at, booking_count)              .duration is end_at - start_at
    Context(user, calendar, local, run, history)
    RuleResult has .passed (bool) and .fail_reason (str or None)

EVERY BookingRecord IN A FIXTURE'S HISTORY MUST CARRY THE SAME `user_id` AS THAT FIXTURE'S \
REQUEST. In the running system, `context.history.bookings` is filtered to the requester before \
the rule ever sees it and never contains a booking made by anyone else — on this resource or any \
other. If the rule under test reads `record.user_id` at all, do not give any `BookingRecord` in \
your fixtures a different one: a fixture that does is testing an input the running system can \
never produce, and a rule that only denies when it sees a foreign `user_id` will pass every test \
you write this way while never denying a single real booking. This applies to every fixture in \
the module — positive cases, boundary cases, and the fail-closed probe below alike.

EVERY ARGUMENT SHOWN ABOVE IS REQUIRED AND NONE OF THEM HAS A DEFAULT. `week_starts_on`, `local` \
and `run` are the three this costs a whole run on: `CalendarContext(now=...)`, `Context(user=..., \
calendar=..., local=...)`, or any `Context(...)` missing `run` is a `TypeError` at the first line \
of every test, so the whole module fails before the rule is called once and the report blames the \
rule for a constructor call the rule does not make. This is the same lesson as the free-name rule \
above — a name or an argument you assumed was supplied for you is a run spent finding out it was \
not.

`LocalFrame` is the booking's local calendar, already resolved by the caller: the three \
`[start, end)` pairs are UTC instants bounding the venue's local day, week and calendar month; \
`weekday` is 0 for Monday; `start_minutes` and `end_minutes` are minutes from local midnight. \
Every pair must be ordered and every datetime UTC, and `end_minutes` must exceed `start_minutes` \
— a frame that does not satisfy those raises at construction, in your fixture, not in the rule. \
For a venue on UTC, a booking on Tuesday 2026-07-21 from 12:00 to 13:00 is:

    LocalFrame(
        day_start=datetime(2026, 7, 21, tzinfo=timezone.utc),
        day_end=datetime(2026, 7, 22, tzinfo=timezone.utc),
        week_start=datetime(2026, 7, 20, tzinfo=timezone.utc),
        week_end=datetime(2026, 7, 27, tzinfo=timezone.utc),
        month_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        month_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
        weekday=1, start_minutes=720, end_minutes=780,
    )

Every datetime must be timezone-aware UTC with a zero offset — `datetime(2026, 3, 2, 9, 0, \
tzinfo=timezone.utc)`. A naive datetime or a non-zero offset raises at construction, so a test \
that uses one fails for a reason that has nothing to do with the rule.

`Context` also enforces that every booking in the history is within one calendar month or a \
rolling week of `now`, whichever is wider. Anchor `now` near the bookings you are writing about, \
or the Context itself raises before the rule is ever called.

Every test needs a whole `Context`, so write ONE helper and call it from each of them, changing \
only what that case is about. Copy this and do not drop an argument from it:

    def make_context(local, now, bookings=(), run=None):
        return Context(
            user=UserContext("user-1"),
            calendar=CalendarContext(week_starts_on=Weekday.MONDAY, now=now),
            local=local,
            run=run or RunContext(local.day_start, local.day_start + timedelta(hours=1), 1),
            history=HistoryContext(bookings=tuple(bookings)),
        )

`Weekday.MONDAY` and `UserContext("user-1")` there are values you are supplying, not defaults you \
are restating: `CalendarContext(now=...)` and `Context(user=None, ...)` are two ways this module \
dies on its own first line. `run`'s own default inside the helper is a real `RunContext`, not a \
placeholder — `Context.run` still has no default of its own, so a helper that passed `run=None` \
straight through would die exactly the same way. Pass your own `run=RunContext(...)` from a test \
that is actually about it; every other test can leave the helper's default alone.

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

   Choose the unusable input from what would actually confuse THIS rule: a context whose `now` \
sits far from the request, an empty history where the rule counts, a request whose resource the \
rule has no record of. Not a history holding a different user's bookings — that is never merely \
confusing, it cannot happen at all (see above), and a probe built from it tests nothing about the \
running system.

   IT MUST BE INPUT THE ENGINE TYPES WILL ACTUALLY BUILD. `BookingRequest` and `BookingRecord` \
reject `start_at >= end_at` at construction, so a "negative duration" or zero-length probe never \
reaches the rule at all — the test dies building its own fixture and is reported as the rule \
failing. Same for a naive datetime, a non-zero offset, and a history outside the Context's own \
window. Unusable means unusable *to this rule*, never malformed to the engine.

5. A CASE WHOSE LOCAL FRAME DOES NOT AGREE WITH THE UTC CLOCK. This is the mistake a generated \
rule is most likely to make: reading `request.start_at.hour` or `request.start_at.weekday()` \
instead of `context.local.start_minutes` or `context.local.weekday`. A rule that does passes every \
test written for a venue on UTC and is wrong for every other venue. So if the rule reads anything \
local at all, pin at least one case where the two disagree — a booking at 23:00 UTC on a Monday \
whose venue is far enough east that it is Tuesday 09:00 locally, so `weekday=1`, \
`start_minutes=540`, and `day_start` is the UTC instant of that local midnight. Assert the rule \
follows the frame and not the UTC clock.

   A venue in `Australia/Sydney` (UTC+10 in July, no DST) makes the numbers concrete. A booking at \
`2026-07-20T23:00:00Z` is `2026-07-21 09:00` local — Tuesday, `weekday=1`, `start_minutes=540` — \
and that date's local midnight, `2026-07-21T00:00:00+10:00`, is `2026-07-20T14:00:00Z`:

    request = BookingRequest(
        "user-1", "court-1",
        datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc),
    )
    local = LocalFrame(
        day_start=datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
        day_end=datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc),
        week_start=datetime(2026, 7, 19, 14, 0, tzinfo=timezone.utc),
        week_end=datetime(2026, 7, 26, 14, 0, tzinfo=timezone.utc),
        month_start=datetime(2026, 6, 30, 14, 0, tzinfo=timezone.utc),
        month_end=datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc),
        weekday=1, start_minutes=540, end_minutes=600,
    )
    context = make_context(local, now=datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc))

   A rule reading `request.start_at.weekday()` sees Monday (`0`); a rule reading \
`context.local.weekday` sees Tuesday (`1`). Build a case that only a rule reading the frame gets \
right, and assert it.

6. RETURN-TYPE CHECKS. Assert that `evaluate` returns a `RuleResult`, and that a refusal carries a \
non-empty `fail_reason`. A rule returning `True`, `None` or a bare string is a real failure mode \
and the engine treats it as a refusal.

7. IF THE RULE READS `context.run`, PIN THE CASE WHERE THE REQUEST ALONE WOULD PASS AND THE RUN \
DOES NOT. Build a `RunContext` that starts well before the request — `booking_count` above 1, \
`duration` over whatever bound the rule enforces — while `request.duration` by itself is well \
within it, and assert the rule denies. This is the one case that tells a rule reading \
`context.run` apart from one reading `request.duration`; without it, a rule reaching for the \
wrong span passes every other test the same way.

   AND THE MIRROR, IF THE RULE DOES NOT READ `context.run`: pin a case with a `RunContext` far \
over any bound the rule enforces, around a request that is well within bounds on its own, and \
assert the rule still passes. A rule that should judge only `request.duration` must never be \
denied by a long run sitting around it.

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
    model: str | None = None,
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
