"""Deterministic ``LLMClient`` for the shape conversation flow.

The stub has no network, subprocess, clock, or randomness. It recognises two deliberately narrow
prompt patterns: ``open at <time>`` changes the opening minute and ``from <time> to <time>`` changes
both bounds. An unqualified ``open at 4`` means 04:00, matching the production agent's ambiguity
policy and making an E2E preview visibly move without pretending this test double understands prose.
"""

from __future__ import annotations

import json
import re

from generation.errors import LLMCallError
from generation.llm import DEFAULT_STUB_MODEL, LLMResponse

from .agent import SYSTEM_PROMPT

__all__ = ["StubShapeLLMClient"]


_OPEN_AT = re.compile(
    r"\bopen\s+at\s+(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?P<meridiem>am|pm)?\b",
    re.IGNORECASE,
)
_FROM_TO = re.compile(
    r"\bfrom\s+(?P<start_hour>\d{1,2})(?::(?P<start_minute>[0-5]\d))?\s*"
    r"(?P<start_meridiem>am|pm)?\s+to\s+(?P<end_hour>\d{1,2})(?::(?P<end_minute>[0-5]\d))?\s*"
    r"(?P<end_meridiem>am|pm)?\b",
    re.IGNORECASE,
)
_EVERY_DAY = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


class StubShapeLLMClient:
    """A canned valid shape response, responsive only to the documented time patterns above."""

    default_model = DEFAULT_STUB_MODEL

    def complete(self, *, system: str, prompt: str, model: str | None = None) -> LLMResponse:
        if system != SYSTEM_PROMPT:
            raise LLMCallError(
                "StubShapeLLMClient does not recognise this system prompt; expected "
                "shape.agent.SYSTEM_PROMPT."
            )

        start, end = _bounds_from_prompt(prompt)
        document = {
            "version": 1,
            "operating_blocks": [
                {
                    "days": _EVERY_DAY,
                    "start_time": _format_time(start),
                    "end_time": _format_time(end),
                    "allowed_durations_mins": [60],
                }
            ],
            "blackout_windows": [],
        }
        envelope = {
            "document": document,
            "summary": (
                f"Open {_format_time(start)}–{_format_time(end)} every day with 60-minute bookings."
            ),
            "question": None,
        }
        return LLMResponse(text=json.dumps(envelope), model=model or self.default_model)


def _bounds_from_prompt(prompt: str) -> tuple[int, int]:
    from_to = _FROM_TO.search(prompt)
    if from_to is not None:
        start = _parse_time(
            from_to["start_hour"], from_to["start_minute"], from_to["start_meridiem"]
        )
        end = _parse_time(from_to["end_hour"], from_to["end_minute"], from_to["end_meridiem"])
        if end <= start:
            end += 12 * 60
        if end <= start:
            end += 12 * 60
        if end <= start + 24 * 60:
            return start, end

    open_at = _OPEN_AT.search(prompt)
    if open_at is not None:
        start = _parse_time(open_at["hour"], open_at["minute"], open_at["meridiem"])
        return start, start + 8 * 60

    return 9 * 60, 17 * 60


def _parse_time(hour_text: str, minute_text: str | None, meridiem: str | None) -> int:
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if meridiem:
        if not 1 <= hour <= 12:
            return 0
        hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
    if not 0 <= hour <= 23:
        return 0
    return hour * 60 + minute


def _format_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
