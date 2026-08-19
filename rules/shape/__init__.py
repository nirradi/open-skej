"""The calendar shape: what a venue offers, structurally.

> A shape says what the venue offers. A rule says who may take it, and how much of it.
> (``ops/plans/stream-10/OVERVIEW.md``)

This package holds the shape document's types, its validator, the projection that turns a shape plus
a local date into the intervals a booking may occupy, and the chat agent that authors a complete
validated document -- see ``.claude/rules/calendar-shape.md`` for the full domain document.

**Pure at its projection boundary.** Standard library only -- ``dataclasses``, ``datetime``, no
third-party dependency and no runtime dependency declared in ``rules/pyproject.toml``. The document,
validator, and projection have no ORM, HTTP, or model dependency; ``agent`` is the narrow authoring
boundary that calls a model through ``generation.llm.LLMClient`` and never changes how a shape is
projected. That is what lets the booking gate, calendar grid, chat preview, and benchmark import one
projection rather than each re-deriving the answer and risking different shape semantics.
"""

from .projection import (
    BlackoutInterval,
    DayProjection,
    InvalidBookingRequestError,
    OfferedStart,
    OperatingInterval,
    ShapeVerdict,
    permits,
    project_day,
)
from .agent import (
    SYSTEM_PROMPT,
    ShapeAgentResponseError,
    ShapeAgentResult,
    build_prompt,
    generate_shape,
    parse_shape_response,
    strip_json_fence,
)
from .stub import StubShapeLLMClient
from .types import DAY_CODES, DAY_NAMES, DEFAULT_SHAPE, BlackoutWindow, OperatingBlock, Shape
from .validate import InvalidShapeError, validate_shape

__all__ = [
    "Shape",
    "OperatingBlock",
    "BlackoutWindow",
    "DAY_CODES",
    "DAY_NAMES",
    "DEFAULT_SHAPE",
    "InvalidShapeError",
    "validate_shape",
    "OperatingInterval",
    "BlackoutInterval",
    "OfferedStart",
    "DayProjection",
    "ShapeVerdict",
    "InvalidBookingRequestError",
    "project_day",
    "permits",
    "SYSTEM_PROMPT",
    "ShapeAgentResult",
    "ShapeAgentResponseError",
    "build_prompt",
    "generate_shape",
    "parse_shape_response",
    "strip_json_fence",
    "StubShapeLLMClient",
]
