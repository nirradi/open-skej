"""Turn a shape conversation into one validated calendar-shape document.

This is deliberately smaller than :mod:`generation`: a shape is data, never executable source.
The only shared boundary is ``generation.llm.LLMClient``. One completion is parsed as a strict JSON
envelope and validated with :func:`shape.validate.validate_shape`; a malformed candidate gets one
correction turn carrying the exact failure and completion. A transport failure is never retried.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from generation.errors import LLMCallError
from generation.llm import LLMClient

from .projection import project_day
from .types import BlackoutWindow, OperatingBlock, Shape
from .validate import InvalidShapeError, validate_shape

__all__ = [
    "SYSTEM_PROMPT",
    "ShapeAgentResult",
    "ShapeAgentResponseError",
    "build_prompt",
    "generate_shape",
    "parse_shape_response",
    "strip_json_fence",
]


SYSTEM_PROMPT = """\
You author calendar shapes for Open-Skej, a system that books shared resources. A shape says what a
venue offers, structurally; it does not make member-specific policy decisions.

Return ONLY one JSON object matching the response envelope below. No prose or markdown fence.

## Response envelope

{
  "document": { ...complete shape document... },
  "summary": "One or two member-neutral sentences explaining the reading.",
  "question": null
}

`document`, `summary`, and `question` are the only envelope keys. `document` is always a complete
shape document, never a patch or a diff. `summary` is a non-empty, member-neutral one- or two-
sentence description suitable for chat and should make a potentially surprising reading explicit.
`question` is either null or one non-empty question. Set it only when the proposed shape would be
unbookable: no offered time at all, or every operating block shorter than its smallest offered
duration. In that case still return the complete valid document and ask how the venue should be
made bookable; the caller uses the question as its do-not-publish signal.

## Shape document schema, version 1

{
  "version": 1,
  "operating_blocks": [
    {
      "days": ["MON", "TUE"],
      "start_time": "18:00",
      "end_time": "20:00",
      "allowed_durations_mins": [20],
      "effective_from": "2026-01-01",
      "effective_to": "2026-05-31"
    }
  ],
  "blackout_windows": [
    {
      "start_time": "19:30",
      "end_time": "19:40",
      "reason": "Break",
      "days": ["MON"],
      "date": null,
      "effective_from": null,
      "effective_to": null
    }
  ]
}

The top-level keys are exactly `version`, `operating_blocks`, and `blackout_windows`. `version` is
the integer 1. Both arrays are required; an empty `operating_blocks` array is valid and means closed
until further notice. Do not add a timezone anywhere: times are the venue's local wall clock and
dates are local calendar dates.

Every operating block has required non-empty `days`, `start_time`, `end_time`, and
`allowed_durations_mins`; `effective_from` and `effective_to` are optional. `days` uses only the
unique codes MON, TUE, WED, THU, FRI, SAT, SUN. Times are 24-hour `HH:MM` strings. A start must be
00:00 through 23:59. `end_time` may exceed `24:00`, but must be after its start and no more than 24
hours later; `26:00` means 02:00 the following local day. Durations are positive integers, sorted in
strict ascending order with no duplicates. Effective dates are `YYYY-MM-DD`, are inclusive at both
ends, and an effective_to may not precede effective_from. Overlapping operating blocks are allowed.

Every blackout has required `start_time`, `end_time`, and a non-empty member-facing `reason`; its
time and optional inclusive effective-date bounds follow the same rules. A blackout has `days` or
`date`, never both. `date` is one `YYYY-MM-DD` local date; `days` is a non-empty unique day-code
list. Omitting both means the blackout applies every day in its effective range.

The calendar projects each block from that block's own opening time, stepping by its smallest
offered duration. It offers each declared duration only when it fits, and a blackout removes a
candidate that overlaps it rather than shifting the grid. A block overlap grants the union of what
the independently anchored blocks offer. A block boundary that only touches remains separate.

Commit to a reasonable reading rather than asking routine clarification questions. In particular,
read “open at 4” as 04:00 and say that reading in the summary so the preview can correct it. Ask
only for an unbookable result as described above.

## Worked requirements examples

Prompt: "I'm a teacher and parents need to schedule slots with me. 20 minute slots from 18 to 20; I
take a break for 10 min at 1930."

{
  "version": 1,
  "operating_blocks": [
    {"days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "start_time": "18:00", "end_time": "20:00", "allowed_durations_mins": [20]}
  ],
  "blackout_windows": [
    {"start_time": "19:30", "end_time": "19:40", "reason": "Break"}
  ]
}

Prompt: "The lab equipment runs from 8 to 5pm, with 3 20 min cooldowns at 10, 13 and 15. Students
can take 30 min slots."

{
  "version": 1,
  "operating_blocks": [
    {"days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "start_time": "08:00", "end_time": "17:00", "allowed_durations_mins": [30]}
  ],
  "blackout_windows": [
    {"start_time": "10:00", "end_time": "10:20", "reason": "Cooldown"},
    {"start_time": "13:00", "end_time": "13:20", "reason": "Cooldown"},
    {"start_time": "15:00", "end_time": "15:20", "reason": "Cooldown"}
  ]
}

Prompt: "The music room is bookable for 1 hour sessions in the morning until 1400 and one or two
hour sessions in the evening until 2200."

{
  "version": 1,
  "operating_blocks": [
    {"days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "start_time": "08:00", "end_time": "14:00", "allowed_durations_mins": [60]},
    {"days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], "start_time": "14:00", "end_time": "22:00", "allowed_durations_mins": [60, 120]}
  ],
  "blackout_windows": []
}
"""


_FENCE = re.compile(
    r"^[ \t]*```[^\n]*\n(?P<body>.*?)(?:\n[ \t]*```[ \t]*$|$)", re.DOTALL | re.MULTILINE
)
_REQUIRED_ENVELOPE_KEYS = frozenset({"document", "summary", "question"})
_MEMBER_SPECIFIC_SUMMARY = re.compile(r"\b(?:you|your|yours)\b", re.IGNORECASE)


class ShapeAgentResponseError(ValueError):
    """A model completion is not the strict shape-agent response envelope."""


@dataclass(frozen=True)
class ShapeAgentResult:
    """One validated complete shape, its visible summary, and an optional do-not-publish question."""

    document: Shape
    summary: str
    question: str | None


def strip_json_fence(text: str) -> str:
    """Unwrap the first fenced JSON response, including one preceded by model prose."""
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, got {type(text).__name__}")
    stripped = text.strip()
    match = _FENCE.search(stripped)
    return match.group("body").strip() if match is not None else stripped


def _response_error(message: str) -> ShapeAgentResponseError:
    return ShapeAgentResponseError(f"response: {message}")


def _summary_is_one_or_two_sentences(summary: str) -> bool:
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", summary.strip()) if part]
    return 1 <= len(sentences) <= 2 and all(part.endswith((".", "!", "?")) for part in sentences)


def parse_shape_response(completion: str) -> ShapeAgentResult:
    """Parse, envelope-check, and validate one model completion."""
    try:
        candidate = strip_json_fence(completion)
    except TypeError as exc:
        raise _response_error(str(exc)) from exc
    try:
        envelope: Any = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise _response_error(f"must be valid JSON: {exc.msg}") from exc

    if not isinstance(envelope, dict):
        raise _response_error(f"must be an object, got {type(envelope).__name__}")
    keys = set(envelope)
    if keys != _REQUIRED_ENVELOPE_KEYS:
        missing = sorted(_REQUIRED_ENVELOPE_KEYS - keys)
        unexpected = sorted(keys - _REQUIRED_ENVELOPE_KEYS)
        details = []
        if missing:
            details.append(f"missing keys {missing!r}")
        if unexpected:
            details.append(f"unexpected keys {unexpected!r}")
        raise _response_error(
            "envelope must contain exactly document, summary, question; " + "; ".join(details)
        )

    summary = envelope["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise _response_error("summary must be a non-empty string")
    summary = summary.strip()
    if not _summary_is_one_or_two_sentences(summary):
        raise _response_error("summary must contain one or two complete sentences")
    if _MEMBER_SPECIFIC_SUMMARY.search(summary):
        raise _response_error("summary must be member-neutral, not addressed to the reader")

    question = envelope["question"]
    if question is not None and (not isinstance(question, str) or not question.strip()):
        raise _response_error("question must be null or a non-empty string")
    if isinstance(question, str):
        question = question.strip()

    document = validate_shape(envelope["document"])
    unbookable = _is_unbookable(document)
    if unbookable and question is None:
        raise _response_error("question must be set when the document is unbookable")
    if not unbookable and question is not None:
        raise _response_error("question must be null when the document is bookable")

    return ShapeAgentResult(document=document, summary=summary, question=question)


def build_prompt(
    conversation: str, *, previous_completion: str | None = None, failure: str | None = None
) -> str:
    """Build one complete-document turn, including unabridged retry feedback when needed."""
    if not conversation or not conversation.strip():
        raise ValueError("conversation must be non-empty")

    parts = [
        "Create the complete calendar-shape response for this conversation:\n\n"
        f"<conversation>\n{conversation.strip()}\n</conversation>"
    ]
    if previous_completion is not None:
        parts.append(
            "Your previous completion failed validation. Return a complete corrected JSON envelope, "
            "not a patch or a diff. The failing completion was:\n\n"
            f"<previous-completion>\n{previous_completion}\n</previous-completion>"
        )
    if failure is not None:
        parts.append(
            "This is the exact validation error; fix it without omitting any required part:\n\n"
            f"<validation-error>\n{failure}\n</validation-error>"
        )
    return "\n\n".join(parts)


def generate_shape(
    conversation: str, *, client: LLMClient, model: str | None = None
) -> ShapeAgentResult:
    """Call the model once, retry one malformed candidate, and return a validated shape result."""
    previous_completion: str | None = None
    failure: str | None = None

    for attempt in range(2):
        try:
            response = client.complete(
                system=SYSTEM_PROMPT,
                prompt=build_prompt(
                    conversation, previous_completion=previous_completion, failure=failure
                ),
                model=model,
            )
        except LLMCallError:
            raise

        try:
            return parse_shape_response(response.text)
        except (InvalidShapeError, ShapeAgentResponseError) as exc:
            if attempt == 1:
                raise
            previous_completion = response.text
            failure = str(exc)

    raise AssertionError("the two-attempt shape-agent loop must return or raise")


def _is_unbookable(shape: Shape) -> bool:
    """Whether no date a valid shape can project has an offered start."""
    for block in shape.operating_blocks:
        for on_date in _representative_dates(block, shape.blackout_windows):
            if project_day(shape, on_date).bookable:
                return False
    return True


def _representative_dates(
    block: OperatingBlock, blackouts: tuple[BlackoutWindow, ...]
) -> tuple[date, ...]:
    """Pick each block weekday from every interval with stable blackout membership."""
    lower = block.effective_from or date.min
    upper = block.effective_to or date.max
    boundaries = {lower}
    upper_exclusive = _next_date(upper)
    if upper_exclusive is not None:
        boundaries.add(upper_exclusive)

    for blackout in blackouts:
        for boundary in (
            blackout.effective_from,
            _next_date(blackout.effective_to) if blackout.effective_to else None,
            blackout.date,
            _next_date(blackout.date) if blackout.date else None,
        ):
            if boundary is not None and lower <= boundary <= upper:
                boundaries.add(boundary)

    ordered = sorted(boundaries)
    candidates: set[date] = set()
    for index, start in enumerate(ordered):
        next_boundary = ordered[index + 1] if index + 1 < len(ordered) else upper_exclusive
        end = next_boundary - timedelta(days=1) if next_boundary is not None else upper
        if start > end:
            continue
        for weekday in block.days:
            offset = (weekday - start.weekday()) % 7
            if offset <= (end - start).days:
                candidate = start + timedelta(days=offset)
                candidates.add(candidate)
    return tuple(sorted(candidates))


def _next_date(value: date | None) -> date | None:
    if value is None or value == date.max:
        return None
    return value + timedelta(days=1)
