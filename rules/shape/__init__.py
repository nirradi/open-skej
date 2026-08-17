"""The calendar shape: what a venue offers, structurally.

> A shape says what the venue offers. A rule says who may take it, and how much of it.
> (``ops/plans/stream-10/OVERVIEW.md``)

This package holds the shape document's types, its validator, and the projection that turns a shape
plus a local date into the intervals a booking may occupy -- see ``.claude/rules/calendar-shape.md``
for the full domain document, including the schema's five settled decisions and the fail-closed
contract.

**Pure by construction.** Standard library only -- ``dataclasses``, ``datetime``, no third-party
dependency and no runtime dependency declared in ``rules/pyproject.toml``. No ORM, no HTTP, no model
call: this package answers exactly one question, "given this shape and this date, what may a booking
occupy", and answers it identically for every caller. That is what lets the booking gate, the
calendar grid, the shape chat's own preview, and the benchmark (10.7) import this one implementation
rather than each re-deriving the answer and risking three different ideas of what a shape means.
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
from .types import DAY_CODES, DAY_NAMES, BlackoutWindow, OperatingBlock, Shape
from .validate import InvalidShapeError, validate_shape

__all__ = [
    "Shape",
    "OperatingBlock",
    "BlackoutWindow",
    "DAY_CODES",
    "DAY_NAMES",
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
]
