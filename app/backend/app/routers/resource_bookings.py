"""Resource-scoped booking endpoints.

These are the Stream 4 booking routes: a booking is made against a specific
Resource of a specific Space, by the authenticated caller, and authorization is
resolved through ``require_space_role`` on the parent Space — the same one place
every Space-scoped route decides access. A member of the Space may book any
Resource in it; roles never live on the Resource.

They sit **alongside** the unscoped Stream 1 routes in ``app.routers.bookings``,
which keep working until task 4.11 deletes them. This module is the "expand" half
of that expand-then-contract: nothing here replaces a route in place, so the
calendar and the E2E suite stay green throughout.

The mapping of outcomes to status codes mirrors the unscoped routes and adds the
two Stream 4 refusals:

* **404 (``detail``)** — the Space is not the caller's, or the Resource / booking
  is not in it. Raised by ``require_space_role`` and the scoped lookups, and a
  404 (never 403) for a foreign Space so the ``public_id`` is not an oracle.
* **422 + ``error: "rule_denied"``** — the rule engine refused; nothing written.
* **409 + ``error: "overlap"``** — the slot is already taken.
* **409 + ``error: "space_archived"``** — the Space is archived, so it takes no
  new bookings. Its existing future bookings stay and remain cancellable.
* **409 + ``error: "already_started"``** — a cancel of a booking already under way.
* **404 / 409 ``error: "not_found"`` / ``"already_cancelled"``** — the cancel
  targets a missing or already-cancelled booking.

**The rule-engine call keeps its Stream 3 shape**: ``evaluate(request)`` read as
``verdict.allowed`` / ``verdict.message``. Only *what* is passed changes — the
real ``user_id`` and ``resource_id`` instead of the unscoped defaults — which is
the acceptance criterion this task is held to.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import (
    BookingAlreadyCancelledError,
    BookingDriver,
    BookingNotFoundError,
    OverlapError,
)
from app.db.models import utcnow
from app.dependencies import get_driver
from app.identity import service
from app.identity.authz import SpaceContext, require_space_role
from app.identity.models import MembershipRole, Resource, User
from app.db.session import get_session
from app.rules_stub import evaluate
from app.schemas import (
    BookingAlreadyCancelled,
    BookingAlreadyStarted,
    BookingConflict,
    BookingCreate,
    BookingDenied,
    BookingNotFound,
    BookingRead,
    BookingSpaceArchived,
)

router = APIRouter(prefix="/spaces/{public_id}/resources/{resource_id}/bookings", tags=["bookings"])

SessionDep = Annotated[Session, Depends(get_session)]
DriverDep = Annotated[BookingDriver, Depends(get_driver)]
MemberContext = Annotated[SpaceContext, Depends(require_space_role(MembershipRole.MEMBER))]

CONFLICT_MESSAGE = (
    "That time has just been taken by another booking."
    " Please refresh the calendar and pick a different slot."
)
NOT_FOUND_MESSAGE = "That booking no longer exists. Refresh the calendar to see what's current."
ALREADY_CANCELLED_MESSAGE = "That booking was already cancelled."
SPACE_ARCHIVED_MESSAGE = "This Space is archived and is no longer taking new bookings."
ALREADY_STARTED_MESSAGE = "This booking has already started and can no longer be cancelled."
RESOURCE_NOT_FOUND_DETAIL = "Resource not found"


@dataclass(frozen=True)
class ResourceBookingContext:
    """A resolved, access-checked (Space, Resource, caller) for a booking route."""

    space_context: SpaceContext
    resource: Resource

    @property
    def resource_id(self) -> int:
        return self.resource.id

    @property
    def user(self) -> User:
        return self.space_context.user

    @property
    def archived(self) -> bool:
        return self.space_context.space.archived_at is not None


def resolve_resource(
    resource_id: int, context: MemberContext, session: SessionDep
) -> ResourceBookingContext:
    """Resolve the addressed Resource within the caller's Space, or 404.

    ``require_space_role`` runs first (it is a dependency of ``MemberContext``),
    so a caller outside the Space is refused with the shared "Space not found"
    404 before this lookup ever runs — a foreign Space and a missing one stay
    indistinguishable. Only once membership is proven does the Resource lookup
    happen, scoped to that Space in one query, so a ``resource_id`` from another
    Space reads as "not found here" rather than confirming it is live elsewhere.
    """
    try:
        resource = service.get_resource(session, context.space, resource_id)
    except service.ResourceNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=RESOURCE_NOT_FOUND_DETAIL)
    return ResourceBookingContext(space_context=context, resource=resource)


ResourceCtx = Annotated[ResourceBookingContext, Depends(resolve_resource)]


def _require_aware(name: str, value: datetime) -> datetime:
    if value.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{name} must include a timezone offset",
        )
    return value


@router.get("", response_model=list[BookingRead])
def list_resource_bookings(
    context: ResourceCtx,
    driver: DriverDep,
    window_start: Annotated[datetime, Query(alias="from")],
    window_end: Annotated[datetime, Query(alias="to")],
    include_cancelled: bool = False,
) -> list[BookingRead]:
    """Bookings on this Resource overlapping the half-open window ``[from, to)``.

    A read, so it works on an archived Space too — archiving closes new bookings,
    not the record of what is on the calendar.
    """
    _require_aware("from", window_start)
    _require_aware("to", window_end)
    if window_start >= window_end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="from must be before to"
        )

    bookings = driver.list_bookings(
        start=window_start,
        end=window_end,
        resource_id=context.resource_id,
        include_cancelled=include_cancelled,
    )
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
def create_resource_booking(
    payload: BookingCreate, context: ResourceCtx, driver: DriverDep
) -> BookingRead | JSONResponse:
    """Book this Resource for the authenticated caller.

    Order is load-bearing: the archived check and the rules run before the driver
    is reached, so a refusal never leaves a partial write. An archived Space is
    refused up front — no rule needs to run to know a closed venue takes no
    bookings.
    """
    if context.archived:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=BookingSpaceArchived(message=SPACE_ARCHIVED_MESSAGE).model_dump(),
        )

    # The rule-engine call keeps its Stream 3 shape; only the ids passed change,
    # from the unscoped defaults to the real caller and Resource.
    verdict = evaluate(
        payload.to_rule_request(user_id=context.user.id, resource_id=context.resource_id)
    )
    if not verdict.allowed:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=BookingDenied(message=verdict.message).model_dump(),
        )

    try:
        booking = driver.create_booking(
            start_at=payload.start_at,
            end_at=payload.end_at,
            user_id=context.user.id,
            resource_id=context.resource_id,
        )
    except OverlapError:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=BookingConflict(message=CONFLICT_MESSAGE).model_dump(),
        )

    return BookingRead.model_validate(booking)


@router.delete(
    "/{booking_id}",
    response_model=BookingRead,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": BookingNotFound},
        status.HTTP_409_CONFLICT: {"model": BookingAlreadyCancelled},
    },
)
def cancel_resource_booking(
    booking_id: int, context: ResourceCtx, driver: DriverDep
) -> BookingRead | JSONResponse:
    """Cancel a booking on this Resource, freeing its interval.

    Allowed on an archived Space: archiving stops new bookings but leaves the
    existing future ones cancellable. Two Stream 4 guards run before the release:
    the booking must belong to *this* Resource (else 404, so a booking id is not
    an oracle across Resources), and it must not have started yet (else 409).

    Not routed through the rule engine — the rules gate acquiring a slot, not
    releasing one, and running them here would let a rule refuse to release a
    booking that predates it.
    """
    try:
        booking = driver.get_booking(booking_id)
    except BookingNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=BookingNotFound(message=NOT_FOUND_MESSAGE).model_dump(),
        )

    # A booking id belonging to another Resource reads as "not found here", the
    # same 404 as one that does not exist — the id discloses nothing across the
    # Resource boundary.
    if booking.resource_id != context.resource_id:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=BookingNotFound(message=NOT_FOUND_MESSAGE).model_dump(),
        )

    if booking.start_at <= utcnow():
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=BookingAlreadyStarted(message=ALREADY_STARTED_MESSAGE).model_dump(),
        )

    try:
        booking = driver.cancel_booking(booking_id)
    except BookingNotFoundError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=BookingNotFound(message=NOT_FOUND_MESSAGE).model_dump(),
        )
    except BookingAlreadyCancelledError:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=BookingAlreadyCancelled(message=ALREADY_CANCELLED_MESSAGE).model_dump(),
        )

    return BookingRead.model_validate(booking)
