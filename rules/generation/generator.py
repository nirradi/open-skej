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
from .llm import LLMClient

__all__ = [
    "generate_rule",
    "build_prompt",
    "strip_code_fence",
    "SYSTEM_PROMPT",
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
    context.local                   LocalFrame, the booking's local calendar (see constraint 4)
    context.run                     RunContext, the back-to-back session this booking joins (see \
constraint 8)

Each BookingRecord has user_id, resource_id, start_at, end_at. It has NO status field.

Return `RuleResult.allow()` when the booking is permitted, or
`RuleResult.deny("...")` when it is not. The deny string is user-facing copy shown verbatim in the \
UI: write it for the person who was just refused, say what the limit is, and say what they can do \
about it. Never put an exception, a class name, or a variable dump in it.

## Hard constraints

1. DO NOT IMPORT ANYTHING FROM THE RULE ENGINE. `BaseRule`, `RuleResult`, `Context`, \
`BookingRequest`, `BookingRecord`, `LocalFrame`, `RunContext` and `Weekday` are FREE NAMES: the \
namespace that loads your source binds them for you. A safety validator runs over your output \
before anything executes it, and `rules` is not on its import allowlist — \
`from rules.interfaces import BaseRule` fails validation on line one and the whole candidate is \
thrown away. The only modules you may import at all are `datetime`, `zoneinfo` and `math`.

   Those eight names are the ONLY free names you get. Everything else you use, you must import: if \
your rule mentions `timedelta`, `datetime`, `date` or `timezone`, begin your source with \
`from datetime import timedelta` (and whichever others you use). This is the single most common \
way a candidate fails — the validator is a syntax check and will happily pass a rule that names \
`timedelta` without importing it, and the rule then dies with `NameError` the moment it loads, \
including in a default argument like `window=timedelta(days=7)`.

2. FAIL CLOSED. Never catch your own exception and return a pass, and never return anything that \
is not a RuleResult. The engine already contains a rule that raises and a rule that returns \
nonsense, and turns both into a refusal — but a rule that swallows its own errors into \
`RuleResult.allow()` defeats that containment silently. It looks like a working rule that simply \
never denies, and the way it is discovered is two people standing on the same court. If you cannot \
establish that a booking is permitted, deny it or let the exception out. Do not write a bare \
`except`.

3. PARAMETERS GO ON THE INSTANCE, in `__init__`, never as module-level constants. A Space that \
allows two bookings a week and one that allows five are the same rule with different arguments. \
EVERY NUMBER THE RULE ENFORCES IS ONE OF THOSE ARGUMENTS — including one the request stated as \
settled fact. The request describes one venue as it is today; the rule type you write is what \
every other venue will configure. "capped at two hours, but one hour at peak, and peak is 17-21 \
but can change" names FOUR parameters, not two: the two limits as much as the two window bounds, \
even though only the window was called changeable. A number still sitting as a literal inside \
`evaluate` is a parameter you failed to lift. Validate the arguments in `__init__` and raise \
ValueError on a nonsensical one. Do not call `super().__init__()` — see constraint 6.

4. EVERY DATETIME IS UTC; LOCAL QUESTIONS ARE ALREADY ANSWERED FOR YOU. Every datetime you see is \
UTC, timezone-aware, with a zero offset; this is enforced at construction, so you may rely on it \
absolutely. Never convert a timezone and never write DST handling — you have no zone to convert \
with, and code that reaches for one is wrong. When your rule needs a LOCAL notion — the venue's \
day, week, month, weekday, or the time of day a booking starts — read it from `context.local`, \
which the caller has already resolved in the venue's own timezone:

    context.local.day_start / day_end       UTC instants bounding this booking's local day
    context.local.week_start / week_end     ... its local week
    context.local.month_start / month_end   ... its local calendar month
    context.local.weekday                   int, Monday = 0
    context.local.start_minutes             minutes from local midnight to the booking's start
    context.local.end_minutes               ... to its end. May exceed 1440 past local midnight

`start_at.weekday()` is a UTC weekday and is almost never what a rule means; use \
`context.local.weekday`. The same goes for `start_at.hour` — use `context.local.start_minutes`. \
To count bookings in the local day, filter `context.history.bookings` to those starting in \
`[day_start, day_end)`.

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

7. NEVER PUT A CLOCK TIME, A CALENDAR DATE, OR A TIMEZONE NAME IN THE DENY STRING. You cannot know \
the reader's timezone, and every datetime you hold is UTC — so any time you print is UTC shown as \
if it were local, and the person reading it has no way to tell. State the limit in relative terms \
instead: a duration, a count, a number of days. If the person needs the specifics, tell them where \
to look — the calendar shows this Space's hours in its own clock. "Bookings can be at most 2 hours \
long" is fine; "closes at 17:00" is not, even though both are true.

8. A RULE DECLARES WHICH SPAN IT JUDGES — CHOOSE ON PURPOSE. `request.duration` is this one \
booking; it is what "no session longer than an hour" means. `context.run.duration` is the \
contiguous, back-to-back session this booking joins **across every Resource**, with this request \
already folded in; it is what "no more than two hours of play in a row" means, and almost always \
what "no more than one hour of peak time" means too. `context.run.start_at`, `context.run.end_at` \
and `context.run.booking_count` describe that same session. Neither span is a default — a \
constraint that says "in a row", "back to back", or "consecutively", or that caps a total a member \
could trivially split into two separate bookings, wants the run.

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
            limit_minutes = int(self.max_duration.total_seconds() // 60)
            return RuleResult.deny(
                f"Bookings can be at most {limit_minutes} minutes long. "
                "Please shorten it and try again."
            )
        return RuleResult.allow()

Note what that deny string does NOT do: it does not say "at most 2 hours". The bound is a \
parameter, so the copy is built from the parameter — a rule whose message names a number its \
`__init__` was given is a rule that lies the moment a venue configures a different one. A \
duration rendered this way is still legal copy under constraint 7; a clock time never is.

A rule that reads a LOCAL notion reads it from `context.local` and never converts anything itself \
— and its deny copy still names no clock time (constraint 7):

class NotBeforeRule(BaseRule):
    \"\"\"Bookings may not start before ``earliest_minutes`` past the venue's local midnight.\"\"\"

    def __init__(self, earliest_minutes):
        if earliest_minutes < 0 or earliest_minutes >= 1440:
            raise ValueError(f"earliest_minutes must be within a day; got {earliest_minutes!r}")
        self.earliest_minutes = earliest_minutes

    def evaluate(self, request, context):
        if context.local.start_minutes < self.earliest_minutes:
            return RuleResult.deny(
                "That is earlier than this space opens. Please check the calendar for "
                "the opening hours and pick a later time."
            )
        return RuleResult.allow()

A rule about consecutive play (constraint 8) reads `context.run`, never `request.duration`:

class MaxConsecutivePlayRule(BaseRule):
    \"\"\"A back-to-back run of bookings, across every Resource, may not exceed ``max_run``.\"\"\"

    def __init__(self, max_run):
        if max_run <= timedelta(0):
            raise ValueError(f"max_run must be positive; got {max_run!r}")
        self.max_run = max_run

    def evaluate(self, request, context):
        if context.run.duration > self.max_run:
            return RuleResult.deny(
                "That would put too much play back to back. Please leave a gap before or "
                "after it, or shorten it."
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
    model: str | None = None,
    previous_source: str | None = None,
    failure: str | None = None,
) -> str:
    """Generate validated rule source for ``description``.

    Raises ``LLMCallError`` if the backend could not produce a completion, and
    ``RuleRejectedError`` if what it produced is not source the safety validator accepts. Both are
    ``GenerationError``; the retry loop cares which, because only the second is worth another try.

    ``previous_source`` and ``failure`` are how the retry loop asks for a *correction* rather than
    another independent attempt: the candidate that did not work and the account of how it did not.
    Both are optional and neither is interpreted here — see ``build_prompt``.

    ``client`` is required and has no default: a module-level default client would make it possible
    to call a model by accident, including from a test that meant to mock one.

    The per-call token, cost and latency metadata lives on the ``LLMResponse`` at the client seam,
    which is where a caller that wants to account for it observes it.
    """
    if not description or not description.strip():
        raise ValueError("description must be a non-empty rule description")

    response = client.complete(
        system=SYSTEM_PROMPT,
        prompt=build_prompt(description, previous_source=previous_source, failure=failure),
        model=model,
    )
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


def build_prompt(
    description: str,
    *,
    previous_source: str | None = None,
    failure: str | None = None,
) -> str:
    """The user turn: the constraint to enforce, plus the last attempt if there was one.

    Everything durable — the contract, the constraints, the worked example — is in the system
    prompt, so the part that changes per call stays this short. The description is delimited
    because it is untrusted text a Space admin typed; the delimiter does not make it safe, and
    nothing generated here runs without a human reading it first.

    On a retry the failing source and the failure are handed back verbatim. Verbatim is the point:
    a validator message names the construct and its line, and a pytest report names the assertion
    and the values that broke it. Summarising either would leave the model to guess at the one
    detail that tells it what to change.

    The failing *tests* are deliberately not included. The pytest output already quotes the
    assertion that failed, and handing the model the suite it must satisfy invites it to write a
    rule shaped around those particular tests rather than around the constraint.
    """
    parts = [
        "Write the rule class for this booking constraint:\n\n"
        f"<constraint>\n{description.strip()}\n</constraint>"
    ]
    if previous_source and previous_source.strip():
        parts.append(
            "Your previous attempt did not work. This is what you wrote:\n\n"
            f"<previous-attempt>\n{previous_source.strip()}\n</previous-attempt>"
        )
    if failure and failure.strip():
        parts.append("This is how it failed:\n\n" f"<failure>\n{failure.strip()}\n</failure>")
    if (previous_source and previous_source.strip()) or (failure and failure.strip()):
        parts.append(
            "Fix the problem and return the complete corrected class, not a patch or a diff. "
            "Return only the Python source."
        )
    else:
        parts.append("Return only the Python source.")
    return "\n\n".join(parts)


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
