"""The Space endpoints.

Every Space here is addressed by ``public_id`` and never by the integer primary
key — see ``app.identity.schemas`` for why, and ``app.identity.authz`` for why a
caller outside a Space gets 404 rather than 403.

The authorization for each route is the ``Depends`` in its signature, not a check
in its body. That is deliberate: a permission test written inside a handler is
one early ``return`` away from being skipped, and it is invisible in the route
table. Declared as a dependency it runs before the handler exists, and reading
the signature tells you the rule.

One route — ``/preview`` — is reachable by any authenticated caller holding the
link, which is the entire point of it. It is the only route in this module
without a ``require_space_role``, and its response is deliberately thin.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.session import get_session
from app.identity import service
from app.identity.authz import SpaceContext, lookup_space, require_space_role, space_not_found
from app.identity.models import MembershipRole, User
from app.identity.schemas import (
    MemberRead,
    MembershipUpdate,
    SpaceCreate,
    SpacePreview,
    SpaceRead,
    SpaceUpdate,
)

router = APIRouter(prefix="/spaces", tags=["spaces"])

SessionDep = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]

MemberContext = Annotated[SpaceContext, Depends(require_space_role(MembershipRole.MEMBER))]
AdminContext = Annotated[SpaceContext, Depends(require_space_role(MembershipRole.ADMIN))]
OwnerContext = Annotated[SpaceContext, Depends(require_space_role(MembershipRole.OWNER))]

ARCHIVED_DETAIL = "This Space is archived and can no longer be changed."
MEMBER_NOT_FOUND_DETAIL = "That user is not a member of this Space."
LAST_OWNER_DETAIL = (
    "This Space must always have at least one owner."
    " Promote another member to owner before changing this one."
)


def _archived() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ARCHIVED_DETAIL)


def _last_owner() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=LAST_OWNER_DETAIL)


def _member_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MEMBER_NOT_FOUND_DETAIL)


@router.post("", response_model=SpaceRead, status_code=status.HTTP_201_CREATED)
def create_space(payload: SpaceCreate, user: CurrentUser, session: SessionDep) -> SpaceRead:
    """Create a Space. The creator becomes its owner.

    The response carries the ``public_id``, which is the shareable link and the
    only handle to this Space that will ever exist — there is no listing endpoint
    to recover it from if the caller discards it.
    """
    space = service.create_space(session, user, name=payload.name, description=payload.description)
    return SpaceRead.build(space, MembershipRole.OWNER)


@router.get("", response_model=list[SpaceRead])
def list_spaces(
    user: CurrentUser, session: SessionDep, include_archived: bool = False
) -> list[SpaceRead]:
    """The Spaces this caller belongs to.

    **Not** a listing of all Spaces — there is no such route, by design. Spaces
    are not discoverable, so this returns memberships and nothing else.
    """
    return [
        SpaceRead.build(space, role)
        for space, role in service.list_spaces_for_user(
            session, user, include_archived=include_archived
        )
    ]


@router.get("/{public_id}", response_model=SpaceRead)
def read_space(context: MemberContext) -> SpaceRead:
    """Full detail, for members only. Non-members get 404, not 403."""
    return SpaceRead.build(context.space, context.role)


@router.get("/{public_id}/preview", response_model=SpacePreview)
def preview_space(public_id: str, user: CurrentUser, session: SessionDep) -> SpacePreview:
    """What someone holding the link sees before they are let in.

    The only Space route without a membership requirement, so its 404 means
    something different from every other 404 in this module: here it genuinely
    says "no such Space", because a caller who reached this route is presumed to
    hold the link already and the id is not a secret from them.

    Name, description and their own status — nothing else. No member list, no
    counts, no bookings.
    """
    space = lookup_space(session, public_id)
    if space is None:
        raise space_not_found()

    return SpacePreview(
        public_id=space.public_id,
        name=space.name,
        description=space.description,
        status=service.preview_status(session, space, user),
    )


@router.patch("/{public_id}", response_model=SpaceRead)
def update_space(payload: SpaceUpdate, context: AdminContext, session: SessionDep) -> SpaceRead:
    """Rename a Space or edit its description. Admin or owner."""
    try:
        space = service.update_space(session, context.space, payload)
    except service.SpaceArchivedError:
        raise _archived()

    return SpaceRead.build(space, context.role)


@router.post("/{public_id}/archive", response_model=SpaceRead)
def archive_space(context: OwnerContext, session: SessionDep) -> SpaceRead:
    """End a Space. Owner only, and there is no delete.

    Restricted more tightly than the other mutations because it is the one
    action with no inverse — there is no un-archive endpoint — and because what
    it means for the bookings already made against the Space is Stream 4's
    question, not one an admin should be able to force early.
    """
    try:
        space = service.archive_space(session, context.space)
    except service.SpaceArchivedError:
        raise _archived()

    return SpaceRead.build(space, context.role)


@router.get("/{public_id}/members", response_model=list[MemberRead])
def list_members(context: MemberContext, session: SessionDep) -> list[MemberRead]:
    """Who is in this Space. Visible to members — outsiders never reach here."""
    return [
        MemberRead.build(membership, user)
        for membership, user in service.list_members(session, context.space)
    ]


@router.patch("/{public_id}/members/{user_id}", response_model=MemberRead)
def update_member(
    user_id: int, payload: MembershipUpdate, context: AdminContext, session: SessionDep
) -> MemberRead:
    """Change a member's role. Refuses to demote the last owner (409)."""
    try:
        membership, user = service.change_member_role(
            session, context.space, target_user_id=user_id, role=payload.role
        )
    except service.SpaceArchivedError:
        raise _archived()
    except service.MemberNotFoundError:
        raise _member_not_found()
    except service.LastOwnerError:
        raise _last_owner()

    return MemberRead.build(membership, user)


@router.delete("/{public_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(user_id: int, context: AdminContext, session: SessionDep) -> Response:
    """Remove a member. Refuses to remove the last owner (409)."""
    try:
        service.remove_member(session, context.space, target_user_id=user_id)
    except service.SpaceArchivedError:
        raise _archived()
    except service.MemberNotFoundError:
        raise _member_not_found()
    except service.LastOwnerError:
        raise _last_owner()

    return Response(status_code=status.HTTP_204_NO_CONTENT)
