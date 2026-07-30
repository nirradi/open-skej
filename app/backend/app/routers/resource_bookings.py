"""Resource-scoped booking endpoints.

These are the only booking routes: a booking is made against a specific
Resource of a specific Space, by the authenticated caller, and authorization is
resolved through ``require_space_role`` on the parent Space — the same one place
every Space-scoped route decides access. A member of the Space may book any
Resource in it; roles never live on the Resource.

They were introduced alongside the unscoped Stream 1 routes in
``app.routers.bookings``, which task 4.11 deleted once the calendar and the E2E
suite were rebuilt on this module. Nothing books against a fixed default
Resource and user any more.

The mapping of outcomes to status codes:

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
* **403 + ``error: "not_yours"``** — a member (not admin/owner) cancelling a
  booking that belongs to someone else. The one bounded exception to the
  404-not-403 rule above: this caller already proved membership and is looking
  at a booking they can already see on the calendar, so there is nothing left
  for a 404 to conceal.

**The rule-engine call's shape changes here, by design (task 4.13b).** It was
``evaluate(request)`` against the module-level ``DEFAULT_CANON``; it is now
``evaluate(request, config, history)`` against a canon assembled from the
Space's own configuration — see ``_space_rule_config`` and
``_load_space_history`` below. The verdict is still read the same way:
``verdict.allowed`` / ``verdict.message``.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from rules import history_window
from sqlalchemy.orm import Session

from app.db import (
    Booking,
    BookingAlreadyCancelledError,
    BookingDriver,
    BookingNotFoundError,
    OverlapError,
)
from app.db.models import utcnow
from app.dependencies import get_driver
from app.identity import service
from app.identity.authz import SpaceContext, require_space_role, role_at_least
from app.identity.models import MembershipRole, Resource, Space, User
from app.db.session import get_session
from app.rules_stub import BookingRequest, SpaceRuleConfig, evaluate
from app.schemas import (
    BookingAlreadyCancelled,
    BookingAlreadyStarted,
    BookingConflict,
    BookingCreate,
    BookingDenied,
    BookingNotFound,
    BookingNotYours,
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
NOT_YOURS_MESSAGE = "You can only cancel your own bookings. Ask an admin if this one needs to go."
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


def _booking_read(booking: Booking, context: ResourceBookingContext) -> BookingRead:
    """Build a ``BookingRead`` for ``context``'s caller.

    The one place a ``Booking`` row and the caller's identity meet: ``mine`` is
    always computed, and ``user_id`` is exposed only to admin and owner, never
    to a plain member. Every route that returns a ``BookingRead`` goes through
    this — see ``BookingRead``'s docstring for why a bare ``model_validate``
    can no longer build one.
    """
    can_see_owner = role_at_least(context.space_context.role, MembershipRole.ADMIN)
    return BookingRead(
        id=booking.id,
        resource_id=booking.resource_id,
        user_id=booking.user_id if can_see_owner else None,
        mine=booking.user_id == context.user.id,
        start_at=booking.start_at,
        end_at=booking.end_at,
        status=booking.status,
        created_at=booking.created_at,
        cancelled_at=booking.cancelled_at,
    )


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


def _space_rule_config(space: Space) -> SpaceRuleConfig:
    """Build this Space's rule configuration for the engine adapter.

    A straight field-for-field copy off the ORM row — ``rules_stub`` stays
    ORM-free (its own module docstring), so this is the one place a ``Space``
    row and ``SpaceRuleConfig`` meet.
    """
    return SpaceRuleConfig(
        timezone=space.timezone,
        opens_at=space.opens_at,
        closes_at=space.closes_at,
        max_duration_minutes=space.max_duration_minutes,
        booking_horizon_days=space.booking_horizon_days,
        max_bookings_per_week=space.max_bookings_per_week,
        max_bookings_per_month=space.max_bookings_per_month,
    )


def _load_space_history(
    session: Session, space: Space, user_id: int, *, now: datetime
) -> tuple[BookingRequest, ...]:
    """This user's confirmed bookings across every Resource in ``space``, capped
    to ``rules.history_window(now)``.

    Only called when the Space's canon actually includes a counting rule — see
    the call site — so a Space with neither cap set costs no query at all, the
    same "no wasted round trip" property the adapter has always documented.
    """
    lower, upper = history_window(now)
    bookings = service.list_user_bookings_in_space(
        session, space, user_id, lower=lower, upper=upper
    )
    return tuple(
        BookingRequest(
            user_id=str(user_id),
            resource_id=str(booking.resource_id),
            start_at=booking.start_at,
            end_at=booking.end_at,
        )
        for booking in bookings
    )


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
    return [_booking_read(booking, context) for booking in bookings]


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
    payload: BookingCreate, context: ResourceCtx, driver: DriverDep, session: SessionDep
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

    space = context.space_context.space
    config = _space_rule_config(space)

    # One instant, shared by the history query and the engine's own clock, so a
    # request straddling a window boundary cannot see one "now" build the
    # history and a slightly later one judge it — which could otherwise reject
    # a just-loaded booking as outside the engine's own history window.
    now = datetime.now(timezone.utc)
    history = (
        _load_space_history(session, space, context.user.id, now=now)
        if config.counts_history
        else ()
    )

    verdict = evaluate(
        payload.to_rule_request(user_id=context.user.id, resource_id=context.resource_id),
        config,
        history,
        now=now,
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

    return _booking_read(booking, context)


@router.delete(
    "/{booking_id}",
    response_model=BookingRead,
    responses={
        status.HTTP_403_FORBIDDEN: {"model": BookingNotYours},
        status.HTTP_404_NOT_FOUND: {"model": BookingNotFound},
        status.HTTP_409_CONFLICT: {"model": BookingAlreadyCancelled},
    },
)
def cancel_resource_booking(
    booking_id: int, context: ResourceCtx, driver: DriverDep
) -> BookingRead | JSONResponse:
    """Cancel a booking on this Resource, freeing its interval.

    Allowed on an archived Space: archiving stops new bookings but leaves the
    existing future ones cancellable.

    Four guards run before the release, and it matters which of them is about
    *authorization* and which is not — this route has a history of getting that
    wrong. ``ResourceCtx`` (via ``require_space_role``) proves only
    *membership*: that the caller belongs to this Space at all. The next guard
    below proves the booking is *on this Resource*. The one after that proves
    it *has not started*. **Not one of those three says anything about whose
    booking this is** — that reads as "authorization happens here" because
    three guards in a row run before the cancel, and the obvious rule (a member
    cancels their own; admin and owner cancel any booking in the Space) was
    never written down as a fourth. ``BookingRead`` has carried ``user_id``
    since task 4.2 and nothing ever compared it to ``context.user.id`` until
    this guard did.

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

    # Ownership: a member may cancel only their own booking; admin and owner
    # may cancel any booking in the Space, the same ladder `require_space_role`
    # already enforces elsewhere. 403, not 404 — see `BookingNotYours` for why
    # this is the one bounded exception to the 404-not-403 rule the rest of the
    # Space API follows.
    if booking.user_id != context.user.id and not role_at_least(
        context.space_context.role, MembershipRole.ADMIN
    ):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=BookingNotYours(message=NOT_YOURS_MESSAGE).model_dump(),
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

    return _booking_read(booking, context)
