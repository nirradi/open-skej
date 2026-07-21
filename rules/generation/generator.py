"""Agent A: turn a natural-language booking constraint into rule source.

``generate_rule("users can only book twice a rolling week")`` returns Python source for a
``BaseRule`` subclass, or raises. It calls the model through the ``LLMClient`` seam, strips the
markdown fence the model usually wraps code in, and runs the result through
``rules.safety.validate_source`` **before returning it** — nothing leaves this module unvalidated,
so no caller can write an unchecked candidate to disk by forgetting a step.

Rejection is a value the retry loop needs, not an accident: ``RuleRejectedError`` carries the
validator's own message, which is what the loop feeds back to the model as the reason to try again.

**Nothing generated is imported by anything.** The output is a string, reviewed by a human and
committed through a PR before it becomes code the booking API runs. That is what keeps a prompt
injection in a rule description out of the request path entirely.

The system prompt below is the load-bearing part of this module. Every constraint it states is one
the validator or the controller enforces anyway — but enforcement without instruction just means
every candidate fails, and a retry budget spent rediscovering a rule that could have been stated
once.
"""

from __future__ import annotations

import re

from rules.safety import UnsafeRuleError, validate_source

from .errors import RuleRejectedError
from .llm import DEFAULT_MODEL, LLMClient

__all__ = [
    "generate_rule",
    "build_prompt",
    "strip_code_fence",
    "SYSTEM_PROMPT",
    "DEFAULT_MODEL",
]


SYSTEM_PROMPT = """\
You write booking rules for Open-Skej, a system that books time on a shared resource such as a \
tennis court. You are given one constraint in plain English and you return one Python class that \
enforces it.

Return ONLY Python source. No explanation, no commentary, no markdown fence.

## The contract

Write a class that inherits from `BaseRule` and implements exactly one method:

    def evaluate(self, request: BookingRequest, context: Context) -> RuleResult

`request` is a BookingRequest with:
    user_id: str, resource_id: str, start_at: datetime, end_at: datetime
    duration: timedelta   (a property: end_at - start_at)

`context` is a Context with:
    context.user.user_id            str
    context.calendar.now            datetime, the current instant
    context.now                     shorthand for the same value
    context.calendar.week_starts_on Weekday, an IntEnum numbered like date.weekday(): MONDAY = 0
    context.history.bookings        tuple of BookingRecord

Each BookingRecord has user_id, resource_id, start_at, end_at. It has NO status field.

Return `RuleResult.allow()` when the booking is permitted, or
`RuleResult.deny("...")` when it is not. The deny string is user-facing copy shown verbatim in the \
UI: write it for the person who was just refused, say what the limit is, and say what they can do \
about it. Never put an exception, a class name, or a variable dump in it.

## Hard constraints

1. DO NOT IMPORT ANYTHING FROM THE RULE ENGINE. `BaseRule`, `RuleResult`, `Context`, \
`BookingRequest`, `BookingRecord` and `Weekday` are FREE NAMES: the namespace that loads your \
source binds them for you. A safety validator runs over your output before anything executes it, \
and `rules` is not on its import allowlist — `from rules.interfaces import BaseRule` fails \
validation on line one and the whole candidate is thrown away. The only modules you may import at \
all are `datetime`, `zoneinfo` and `math`.

2. FAIL CLOSED. Never catch your own exception and return a pass, and never return anything that \
is not a RuleResult. The engine already contains a rule that raises and a rule that returns \
nonsense, and turns both into a refusal — but a rule that swallows its own errors into \
`RuleResult.allow()` defeats that containment silently. It looks like a working rule that simply \
never denies, and the way it is discovered is two people standing on the same court. If you cannot \
establish that a booking is permitted, deny it or let the exception out. Do not write a bare \
`except`.

3. PARAMETERS GO ON THE INSTANCE, in `__init__`, never as module-level constants. A Space that \
allows two bookings a week and one that allows five are the same rule with different arguments. \
Validate the arguments in `__init__` and raise ValueError on a nonsensical one. Do not call \
`super().__init__()` — see constraint 6.

4. EVERY DATETIME IS UTC, timezone-aware, with a zero offset; this is enforced at construction, so \
you may rely on it absolutely. `start_at.hour` is a UTC hour and `start_at.weekday()` is a UTC \
weekday. Do NOT convert timezones, do NOT accept or infer a local timezone, and do NOT write any \
DST handling — there are no DST cases here, and code that reaches for one is code that is wrong.

5. EVERYTHING IN `context.history.bookings` COUNTS. It arrives already filtered and already capped \
to at most one calendar month by the caller. There is no status field, no cancellation flag and no \
way to infer one; do not try to exclude anything from the count. Do not assume history reaches \
further back than a month. When a rule counts bookings in a window, count the request itself too: \
with a limit of two and two bookings already in the window, the third is refused.

6. THE VALIDATOR ALSO REJECTS: any attribute beginning with `__` (so no `super().__init__()`, no \
`obj.__class__`), decorators of any kind (no `@property`, no `@dataclass`, no `@staticmethod`), \
`while` loops, `global` and `nonlocal`, and the names `exec`, `eval`, `compile`, `open`, \
`getattr`, `globals`, `locals`, `vars` and `dir`. Use a plain `for` over the history, or a \
comprehension whose every name is one your own source defines.

## Style

Write it the way this hand-written rule is written — this is the reference:

class MaxDurationRule(BaseRule):
    \"\"\"Bookings may not run longer than ``max_duration``. The bound is inclusive.\"\"\"

    def __init__(self, max_duration):
        if max_duration <= timedelta(0):
            raise ValueError(f"max_duration must be positive; got {max_duration!r}")
        self.max_duration = max_duration

    def evaluate(self, request, context):
        if request.duration > self.max_duration:
            return RuleResult.deny(
                "Bookings can be at most 2 hours long. Please shorten it and try again."
            )
        return RuleResult.allow()

Name the class for what it enforces, ending in `Rule`. Give it a docstring saying what it decides \
and whether each bound is inclusive or exclusive. Half-open windows `[start, end)` everywhere, so \
a booking on a boundary is counted once, on the side it starts. Keep it to one class.\
"""


_FENCE = re.compile(
    r"^[ \t]*```[^\n]*\n(?P<body>.*?)(?:^[ \t]*```[ \t]*$|\Z)",
    re.DOTALL | re.MULTILINE,
)


def generate_rule(
    description: str,
    *,
    client: LLMClient,
    model: str = DEFAULT_MODEL,
) -> str:
    """Generate validated rule source for ``description``.

    Raises ``LLMCallError`` if the backend could not produce a completion, and
    ``RuleRejectedError`` if what it produced is not source the safety validator accepts. Both are
    ``GenerationError``; the retry loop cares which, because only the second is worth another try.

    ``client`` is required and has no default: a module-level default client would make it possible
    to call a model by accident, including from a test that meant to mock one.

    The per-call token, cost and latency metadata lives on the ``LLMResponse`` at the client seam,
    which is where a caller that wants to account for it observes it.
    """
    if not description or not description.strip():
        raise ValueError("description must be a non-empty rule description")

    response = client.complete(system=SYSTEM_PROMPT, prompt=build_prompt(description), model=model)
    source = strip_code_fence(response.text)

    if not source.strip():
        raise RuleRejectedError(
            "The model returned no Python source at all.",
            source=source,
        )

    try:
        validate_source(source)
    except UnsafeRuleError as exc:
        # The validator's message names the construct and its line. It is passed through verbatim:
        # that detail is exactly what lets the model fix the candidate on the next attempt.
        raise RuleRejectedError(str(exc), source=source) from exc

    return source


def build_prompt(description: str) -> str:
    """The user turn: the constraint to enforce, and nothing else.

    Everything durable — the contract, the constraints, the worked example — is in the system
    prompt, so the part that changes per call stays this short. The description is delimited
    because it is untrusted text a Space admin typed; the delimiter does not make it safe, and
    nothing generated here runs without a human reading it first.
    """
    return (
        "Write the rule class for this booking constraint:\n\n"
        f"<constraint>\n{description.strip()}\n</constraint>\n\n"
        "Return only the Python source."
    )


def strip_code_fence(text: str) -> str:
    """Return the code in ``text``, with any surrounding markdown fence removed.

    Models fence code far more often than not, and they fence it inconsistently: ```` ``` ````,
    ```` ```python ````, ```` ```py ````, sometimes after a line of prose. The first fenced block
    wins, since the prose that precedes it is never the rule.

    An *unterminated* fence — the answer was cut off mid-block — yields everything after the
    opening line rather than nothing. That source is very likely truncated and will fail to parse,
    which is a rejection the loop can act on and feed back; returning the raw text with a stray
    ```` ``` ```` in it would fail for a reason that describes the fence rather than the answer.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, got {type(text).__name__}")
    match = _FENCE.search(text)
    if match is None:
        return text.strip()
    return match.group("body").strip()
