"""The booking endpoints.

Two responsibilities live here and nowhere else: adapting HTTP to the data layer,
and mapping the two distinct kinds of "no" onto distinct status codes.

* **422 + ``error: "rule_denied"``** — the rule engine refused. The booking was
  never offered to the driver, so nothing was written.
* **409 + ``error: "overlap"``** — the rules were fine but the resource is
  already taken for part of that interval.

The client needs to tell these apart because they call for different UI: a denial
is the user's own request being out of bounds (shorten it, move it), a conflict is
a race with somebody else (the calendar is stale and worth refetching).
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.db import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID, BookingDriver, OverlapError
from app.dependencies import get_driver
from app.rules_stub import evaluate
from app.schemas import BookingConflict, BookingCreate, BookingDenied, BookingRead

router = APIRouter(prefix="/bookings", tags=["bookings"])

CONFLICT_MESSAGE = (
    "That time has just been taken by another booking."
    " Please refresh the calendar and pick a different slot."
)

DriverDep = Annotated[BookingDriver, Depends(get_driver)]


def _require_aware(name: str, value: datetime) -> datetime:
    """Reject a naive timestamp instead of guessing an offset for it.

    The driver raises ``ValueError`` on naive input, which would surface as a 500.
    Catching it here turns an operator-facing crash into a client-facing 400.
    """
    if value.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{name} must include a timezone offset",
        )
    return value


@router.get("", response_model=list[BookingRead])
def list_bookings(
    driver: DriverDep,
    window_start: Annotated[datetime, Query(alias="from")],
    window_end: Annotated[datetime, Query(alias="to")],
) -> list[BookingRead]:
    """Bookings overlapping the half-open window ``[from, to)``.

    Overlapping, not contained: a booking that straddles the edge of the week the
    calendar is showing must still be drawn, or its slot would look free.

    Cancelled bookings are excluded — the calendar shows what is live. Task 1.4
    adds the flag that lets a caller ask for them.
    """
    _require_aware("from", window_start)
    _require_aware("to", window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="from must be before to",
        )

    bookings = driver.list_bookings(start=window_start, end=window_end)
    return [BookingRead.model_validate(booking) for booking in bookings]


@router.post(
    "",
    response_model=BookingRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_409_CONFLICT: {"model": BookingConflict},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": BookingDenied},
    },
)
def create_booking(payload: BookingCreate, driver: DriverDep) -> BookingRead | JSONResponse:
    """Create a booking, subject to the rule engine and the overlap invariant."""
    # Order matters: the rules run first and the driver is only reached if they
    # pass, so a denial cannot leave a partial write behind.
    verdict = evaluate(
        payload.to_rule_request(user_id=DEFAULT_USER_ID, resource_id=DEFAULT_RESOURCE_ID)
    )
    if not verdict.allowed:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=BookingDenied(message=verdict.message).model_dump(),
        )

    try:
        booking = driver.create_booking(start_at=payload.start_at, end_at=payload.end_at)
    except OverlapError:
        # The driver's own message names raw ISO timestamps, which is right for a
        # log and wrong for a user, so the friendly copy is written here.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=BookingConflict(message=CONFLICT_MESSAGE).model_dump(),
        )

    return BookingRead.model_validate(booking)
