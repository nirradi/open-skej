"""Pydantic schemas for the booking HTTP API.

Kept separate from the SQLAlchemy models in ``app.db.models``: those describe how
a booking is stored, these describe what crosses the wire. Task 1.3 wires them to
the endpoints.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.db.models import BookingStatus
from app.rules_stub import BookingRequest


class BookingCreate(BaseModel):
    """The body of ``POST /bookings``.

    Both timestamps must carry an offset. A naive datetime is rejected rather
    than assumed to be UTC, mirroring the data layer's ``UtcDateTime``: guessing
    would silently shift a booking by the client's offset.
    """

    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def _check_interval(self) -> "BookingCreate":
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("start_at and end_at must include a timezone offset")
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        return self

    def to_rule_request(self, *, user_id: str, resource_id: str) -> BookingRequest:
        """Adapt to the rule engine's input model."""
        return BookingRequest(
            user_id=user_id,
            resource_id=resource_id,
            start_at=self.start_at,
            end_at=self.end_at,
        )


class BookingRead(BaseModel):
    """A stored booking as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    resource_id: str
    user_id: str
    start_at: datetime
    end_at: datetime
    status: BookingStatus
    created_at: datetime
    cancelled_at: datetime | None = None


class BookingDenied(BaseModel):
    """The 422 body for a rule-engine denial.

    ``message`` is the rule engine's user-facing copy and is meant to be rendered
    verbatim in the UI.

    ``error`` is a machine-readable discriminator. The status code alone is not
    quite enough: FastAPI also returns 422 for request-validation failures, whose
    body is ``{"detail": [...]}``. Keying off ``error`` lets the client tell a
    friendly rule denial from a malformed request without pattern-matching on the
    body's shape.
    """

    error: Literal["rule_denied"] = "rule_denied"
    message: str


class BookingConflict(BaseModel):
    """The 409 body for an overlap rejected by the data layer.

    Deliberately a different status *and* a different ``error`` value from
    :class:`BookingDenied`: a rule denial means "you may not book this", a
    conflict means "someone else got there first". The UI reacts differently to
    each — the second is worth a calendar refresh, the first is not.
    """

    error: Literal["overlap"] = "overlap"
    message: str
