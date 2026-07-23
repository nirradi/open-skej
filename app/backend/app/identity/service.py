"""Space and membership operations, with the invariants that outlive any handler.

The router adapts HTTP; this module owns the rules that must hold no matter who
is asking. Two of them are worth stating up front.

**A Space always has at least one owner.** Only an owner may archive a Space, so
a Space with no owner is permanently unarchivable and unmanageable — there is no
global superuser to repair it and no ownership-transfer endpoint, both by design.
The invariant is therefore enforced here rather than in the router, because it
has to hold for the ``PATCH`` path and the ``DELETE`` path and anything task 2.6
or 2.7 adds later, and a check that lives in one handler protects only that
handler.

**Archiving is not deletion.** Reads of an archived Space keep working; every
mutation is refused with 409. An archive is a record that something is finished,
and a record you cannot read is not much of a record.

Exceptions here are plain domain errors, translated to status codes by the
router — the same split ``app.db.driver`` uses with ``OverlapError``.
"""

from datetime import time
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import utcnow
from app.identity.authz import role_at_least
from app.identity.models import (
    AccessRequestStatus,
    InvitationStatus,
    MembershipRole,
    Resource,
    Space,
    SpaceAccessRequest,
    SpaceInvitation,
    SpaceMembership,
    User,
)
from app.identity.schemas import PreviewStatus, ResourceUpdate, SpaceUpdate

# The name the auto-created first Resource is given. A fresh Space is a venue with
# one bookable calendar rather than an empty shell, so the admin's primary flow
# never meets an empty state and the schema never has to represent a Space with no
# Resource even though it could.
FIRST_RESOURCE_NAME = "Main"


class SpaceArchivedError(Exception):
    """A mutation was attempted on a Space that has been archived."""


class ResourceNotFoundError(Exception):
    """No Resource with that id belongs to this Space.

    Raised for a resource id that names nothing *and* for one that names a
    Resource in another Space — the two are the same outcome by design. The
    lookup is a single query scoped to ``space_id``, so a foreign id falls out as
    "not found" on the same code path and in the same time as a missing one, and
    the integer id discloses nothing about whether it is live elsewhere. This is
    the Space-level 404-not-403 rule extended one level down to the Resource.
    """


class ResourceArchivedError(Exception):
    """A mutation was attempted on a Resource that has been archived."""


class MemberNotFoundError(Exception):
    """The addressed user has no membership in this Space."""


class LastOwnerError(Exception):
    """The change would leave the Space with no owner at all."""


class AlreadyMemberError(Exception):
    """The requester is already inside the Space they are asking to join."""


class DuplicatePendingRequestError(Exception):
    """This user already has a request awaiting a decision on this Space."""


class AccessRequestNotFoundError(Exception):
    """No request with that id belongs to this Space."""


class AccessRequestAlreadyDecidedError(Exception):
    """The request has already been approved or denied.

    Refused rather than treated as idempotent. A second approval would overwrite
    ``decided_by_user_id`` and ``decided_at``, rewriting who let this person in —
    and an admin re-approving a request another admin denied a minute earlier is
    far more likely to be a stale review queue than a considered reversal.
    """


class InvitedUserAlreadyMemberError(Exception):
    """The invited address already belongs to a member of this Space.

    Distinct from :class:`AlreadyMemberError`, which is about the *caller*. Here
    the caller is an admin and the subject is somebody else, so the two produce
    different copy even though both are 409s.
    """


class DuplicatePendingInvitationError(Exception):
    """This address already has an invitation awaiting acceptance on this Space."""


class InvitationNotFoundError(Exception):
    """No invitation with that id belongs to this Space."""


class InvitationAlreadyResolvedError(Exception):
    """The invitation has already been accepted or revoked.

    Refused rather than treated as idempotent, matching
    :class:`AccessRequestAlreadyDecidedError`. Revoking an *accepted* invitation
    is the case that makes silence dangerous: the invitee is already a member, so
    a 204 would tell the admin they had removed access when the membership is
    untouched — and the fix they actually want is ``DELETE .../members/{id}``.
    A no-op 204 would hide that distinction behind a success.
    """


class InvitationRoleTooHighError(Exception):
    """The inviter tried to invite at a role above their own.

    Without this, "invite a user" is a privilege-escalation primitive: an admin
    could invite an address at ``owner``, and — since an invitation is claimed by
    whoever proves control of the address — invite *themselves* at their own
    second address to obtain ownership. The membership routes already forbid an
    admin granting ``owner`` directly (see :class:`OwnerAuthorityRequiredError`);
    an invitation reaching the same end by a longer route would make that rule
    decorative.
    """


class OwnerAuthorityRequiredError(Exception):
    """Only an owner may grant the owner role or act on an existing owner.

    Without this, making "manage members" an admin capability is a privilege
    escalation rather than a delegation: an admin could ``PATCH`` their own
    membership to ``owner``, and from there archive the Space and demote the
    person who created it. An admin could equally demote or remove an existing
    owner outright, so long as a second owner existed to satisfy the last-owner
    check — so that check alone does not contain this.

    Managing *members and admins* is genuinely delegable and remains admin+. The
    owner role is the one boundary an admin must not be able to cross unaided.
    """


def _require_active(space: Space) -> None:
    if space.archived_at is not None:
        raise SpaceArchivedError(space.public_id)


def _lock_owners(session: Session, space_id: int) -> list[int]:
    """The user ids of this Space's owners, with their rows locked until commit.

    ``FOR UPDATE`` is the whole point, and a plain transaction would not be
    enough. Under Postgres' default READ COMMITTED, two concurrent demotions of
    two different owners would each read "there are 2 owners", each conclude the
    demotion is safe, and both commit — leaving zero owners. Neither transaction
    ever sees the other's uncommitted write, so neither can notice.

    Locking every owner row makes the second transaction block on the first
    rather than read around it. When it wakes, it re-reads the committed state,
    sees one owner remaining, and is refused. The lock covers the *set* of owners
    rather than just the target for exactly that reason: the conflict is between
    two different rows, so locking only the row being changed would not make the
    two transactions collide at all.
    """
    return list(
        session.execute(
            select(SpaceMembership.user_id)
            .where(
                SpaceMembership.space_id == space_id,
                SpaceMembership.role == MembershipRole.OWNER,
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )


def _load_membership(session: Session, space_id: int, user_id: int) -> Optional[SpaceMembership]:
    """This user's membership, re-read from the database rather than the identity map.

    ``populate_existing`` matters here: these lookups happen *after* the owner
    lock is taken, and the point of reading then is to see what other
    transactions committed while we waited. A cached instance from earlier in the
    session would hand back the stale role and defeat the lock.
    """
    return session.execute(
        select(SpaceMembership)
        .where(
            SpaceMembership.space_id == space_id,
            SpaceMembership.user_id == user_id,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def create_space(
    session: Session, creator: User, *, name: str, description: Optional[str]
) -> Space:
    """Create a Space, make its creator the owner, and give it one Resource, atomically.

    Three writes share one transaction. The owner membership because a Space with
    no owner is unrecoverable — nobody could archive it, manage it, or be added to
    it. The first Resource because a Space is a *venue*: a fresh one with no
    bookable calendar is a dead end, and creating it here means the product never
    produces an empty Space even though the schema could represent one. If any
    write fails, none of them survives.
    """
    space = Space(name=name, description=description, created_by_user_id=creator.id)
    session.add(space)
    session.flush()

    session.add(SpaceMembership(space_id=space.id, user_id=creator.id, role=MembershipRole.OWNER))
    session.add(Resource(space_id=space.id, name=FIRST_RESOURCE_NAME))
    session.commit()
    return space


def list_spaces_for_user(
    session: Session, user: User, *, include_archived: bool
) -> list[tuple[Space, MembershipRole]]:
    """Every Space this user belongs to, with their role in each.

    The join to ``space_memberships`` is what makes this safe: it is not a list
    of Spaces filtered by permission afterwards, it is a list of *memberships*,
    so a Space the caller has no row for cannot appear however the query is
    later edited.
    """
    query = (
        select(Space, SpaceMembership.role)
        .join(SpaceMembership, SpaceMembership.space_id == Space.id)
        .where(SpaceMembership.user_id == user.id)
        .order_by(Space.created_at, Space.id)
    )
    if not include_archived:
        query = query.where(Space.archived_at.is_(None))

    return [(space, role) for space, role in session.execute(query).all()]


def update_space(session: Session, space: Space, payload: SpaceUpdate) -> Space:
    """Apply a partial update. Omitted fields are left alone."""
    _require_active(space)

    fields = payload.model_fields_set
    if "name" in fields and payload.name is not None:
        space.name = payload.name
    # An explicit null clears the description; absence leaves it untouched. The
    # schema rejects a null name, so only description can be cleared this way.
    if "description" in fields:
        space.description = payload.description

    session.commit()
    return space


def archive_space(session: Session, space: Space) -> Space:
    """End a Space. There is no delete, and no un-archive.

    Re-archiving is refused rather than treated as a no-op: the caller believes
    they are ending something live, and silently succeeding would hide that
    somebody else already did it — along with *when*, which ``archived_at``
    would otherwise be quietly overwritten to lose.
    """
    _require_active(space)
    space.archived_at = utcnow()
    session.commit()
    return space


def list_resources(session: Session, space: Space, *, include_archived: bool) -> Sequence[Resource]:
    """The Resources in this Space, oldest first.

    Scoped to ``space_id``, so this is a list of *this* Space's calendars and can
    never surface another tenant's however the query is later edited — the same
    shape that keeps ``list_members`` safe. Reads work on an archived Space, so
    this does not check ``_require_active``.
    """
    query = (
        select(Resource)
        .where(Resource.space_id == space.id)
        .order_by(Resource.created_at, Resource.id)
    )
    if not include_archived:
        query = query.where(Resource.archived_at.is_(None))

    return session.execute(query).scalars().all()


def get_resource(session: Session, space: Space, resource_id: int) -> Resource:
    """One Resource of this Space, or raise :class:`ResourceNotFoundError`.

    The ``space_id`` term is the access control, not a convenience: a Resource id
    belonging to another Space returns nothing here and so is indistinguishable
    from one that does not exist — see the exception's docstring for why that
    identity is the point. One query, so both also take the same time.
    """
    resource = session.execute(
        select(Resource).where(
            Resource.id == resource_id,
            Resource.space_id == space.id,
        )
    ).scalar_one_or_none()
    if resource is None:
        raise ResourceNotFoundError(resource_id)
    return resource


def create_resource(
    session: Session,
    space: Space,
    *,
    name: str,
    opens_at: Optional[time] = None,
    closes_at: Optional[time] = None,
    slot_minutes: Optional[int] = None,
) -> Resource:
    """Add a bookable calendar to this Space.

    Refused on an archived Space: a finished venue takes no new calendars, the
    same rule every other mutation here follows. The operating-hours columns are
    stored as given — local wall-clock values with no zone resolution, which is a
    boundary concern and not this function's.
    """
    _require_active(space)

    resource = Resource(
        space_id=space.id,
        name=name,
        opens_at=opens_at,
        closes_at=closes_at,
        slot_minutes=slot_minutes,
    )
    session.add(resource)
    session.commit()
    return resource


def update_resource(
    session: Session, space: Space, *, resource_id: int, payload: ResourceUpdate
) -> Resource:
    """Apply a partial update to a Resource. Omitted fields are left alone.

    An explicit null clears an operating-hours column back to "no restriction";
    absence leaves it untouched, which is why this reads ``model_fields_set``
    rather than the values. Refused on an archived Space (409) and on an archived
    Resource (409) — a retired calendar is history, not something to reconfigure.
    """
    _require_active(space)
    resource = get_resource(session, space, resource_id)
    if resource.archived_at is not None:
        raise ResourceArchivedError(resource_id)

    fields = payload.model_fields_set
    if "name" in fields and payload.name is not None:
        resource.name = payload.name
    if "opens_at" in fields:
        resource.opens_at = payload.opens_at
    if "closes_at" in fields:
        resource.closes_at = payload.closes_at
    if "slot_minutes" in fields:
        resource.slot_minutes = payload.slot_minutes

    session.commit()
    return resource


def archive_resource(session: Session, space: Space, *, resource_id: int) -> Resource:
    """Retire a Resource without deleting it, matching the Space's own end-state.

    There is no delete and no un-archive. Re-archiving is refused rather than
    treated as a no-op, mirroring :func:`archive_space`: the caller believes they
    are retiring something live, and silently succeeding would overwrite
    ``archived_at`` and lose *when* it was actually retired.
    """
    _require_active(space)
    resource = get_resource(session, space, resource_id)
    if resource.archived_at is not None:
        raise ResourceArchivedError(resource_id)

    resource.archived_at = utcnow()
    session.commit()
    return resource


def list_members(session: Session, space: Space) -> Sequence[tuple[SpaceMembership, User]]:
    """Everyone in this Space, oldest membership first."""
    return session.execute(
        select(SpaceMembership, User)
        .join(User, User.id == SpaceMembership.user_id)
        .where(SpaceMembership.space_id == space.id)
        .order_by(SpaceMembership.created_at, SpaceMembership.id)
    ).all()


def change_member_role(
    session: Session,
    space: Space,
    *,
    target_user_id: int,
    role: MembershipRole,
    actor_role: MembershipRole,
) -> tuple[SpaceMembership, User]:
    """Set a member's role, refusing to demote the last owner.

    The owner lock is taken *before* the membership is read, so the check and the
    write sit inside one serialised critical section. Reading first and locking
    afterwards would leave exactly the race the lock exists to close.

    ``actor_role`` is required because the route's admin+ gate is not sufficient
    on its own — see :class:`OwnerAuthorityRequiredError`.
    """
    _require_active(space)

    owners = _lock_owners(session, space.id)

    membership = _load_membership(session, space.id, target_user_id)
    if membership is None:
        raise MemberNotFoundError(target_user_id)

    # Granting owner, or touching someone who already is one, takes owner
    # authority. Checked after the membership load so a non-member still gets
    # "no such member" rather than a permission error that would reveal who is
    # and is not in the Space.
    touches_ownership = role is MembershipRole.OWNER or membership.role is MembershipRole.OWNER
    if touches_ownership and actor_role is not MembershipRole.OWNER:
        raise OwnerAuthorityRequiredError(target_user_id)

    demoting_the_last_owner = role is not MembershipRole.OWNER and owners == [target_user_id]
    if demoting_the_last_owner:
        raise LastOwnerError(target_user_id)

    membership.role = role
    session.commit()

    user = session.execute(select(User).where(User.id == target_user_id)).scalar_one()
    return membership, user


def remove_member(
    session: Session, space: Space, *, target_user_id: int, actor_role: MembershipRole
) -> None:
    """Remove a member, refusing to remove the last owner.

    A membership row *is* deleted here, which is the one exception to this
    schema's "nothing is ever deleted" rule. Access requests and invitations keep
    their decided rows because those are a decision history worth auditing; a
    membership is current state, and a revoked one that lingered would have to be
    excluded from every permission query forever after.

    ``actor_role`` gates removal of an owner — see
    :class:`OwnerAuthorityRequiredError`. The last-owner check alone would not
    stop an admin evicting an owner whenever a second owner happened to exist.
    """
    _require_active(space)

    owners = _lock_owners(session, space.id)

    membership = _load_membership(session, space.id, target_user_id)
    if membership is None:
        raise MemberNotFoundError(target_user_id)

    if membership.role is MembershipRole.OWNER and actor_role is not MembershipRole.OWNER:
        raise OwnerAuthorityRequiredError(target_user_id)

    if owners == [target_user_id]:
        raise LastOwnerError(target_user_id)

    session.delete(membership)
    session.commit()


def _pending_request_id(session: Session, space_id: int, user_id: int) -> Optional[int]:
    return session.execute(
        select(SpaceAccessRequest.id).where(
            SpaceAccessRequest.space_id == space_id,
            SpaceAccessRequest.user_id == user_id,
            SpaceAccessRequest.status == AccessRequestStatus.PENDING,
        )
    ).scalar_one_or_none()


def request_access(
    session: Session, space: Space, user: User, *, message: Optional[str]
) -> SpaceAccessRequest:
    """Ask to be let into a Space, on the strength of holding its link.

    Three things are refused, each for a different reason: an archived Space has
    nobody left to review the queue, an existing member has nothing to ask for,
    and a second *pending* request would only duplicate the first in the admin's
    queue. A previously **denied** request is deliberately not among them — the
    partial unique index constrains pending rows only, precisely so that someone
    turned down in March can ask again in June.

    The pre-check and the ``IntegrityError`` handler are not redundant. The check
    produces the useful error for the ordinary case — a user double-clicking the
    button — while the index is what actually holds under two concurrent requests,
    where both transactions can pass the check before either commits. Catching
    the violation converts that race into the same 409 the slower path returns,
    rather than a 500.
    """
    _require_active(space)

    if _load_membership(session, space.id, user.id) is not None:
        raise AlreadyMemberError(user.id)

    if _pending_request_id(session, space.id, user.id) is not None:
        raise DuplicatePendingRequestError(user.id)

    request = SpaceAccessRequest(space_id=space.id, user_id=user.id, message=message)
    session.add(request)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise DuplicatePendingRequestError(user.id)

    return request


def list_access_requests(
    session: Session, space: Space, *, status: Optional[AccessRequestStatus]
) -> Sequence[tuple[SpaceAccessRequest, User]]:
    """This Space's requests, oldest first, optionally filtered by status.

    Unfiltered by default rather than pending-only: the decided rows are kept as
    history exactly so an admin can see that someone was denied before, and
    hiding them by default would waste the retention the schema pays for.
    """
    query = (
        select(SpaceAccessRequest, User)
        .join(User, User.id == SpaceAccessRequest.user_id)
        .where(SpaceAccessRequest.space_id == space.id)
        .order_by(SpaceAccessRequest.created_at, SpaceAccessRequest.id)
    )
    if status is not None:
        query = query.where(SpaceAccessRequest.status == status)

    return session.execute(query).all()


def decide_access_request(
    session: Session,
    space: Space,
    *,
    request_id: int,
    approve: bool,
    decider: User,
) -> tuple[SpaceAccessRequest, User]:
    """Approve or deny a request. On approval, the membership is part of the same commit.

    **This is the invariant the function exists for.** An approved request whose
    membership row never landed is the worst outcome available here: the
    requester is told they are in, the admin sees the queue cleared, and every
    permission check still says no — with no pending row left for anyone to
    notice, since the request is now decided. So the status stamp and the
    ``INSERT`` into ``space_memberships`` share one transaction and one
    ``commit``. If the insert violates the unique index, the whole transaction
    rolls back and the request stays ``pending``, which is a state the system can
    recover from by simply approving again.

    ``with_for_update`` closes the same race the last-owner lock does. Under READ
    COMMITTED two admins could both read the request as pending and both proceed;
    the second would then either duplicate the membership or overwrite the first
    admin's decision stamp. Locking the row makes the second wait, re-read a
    decided request, and be refused.

    The membership is only inserted if one does not already exist, which is not
    defensive padding — an invitation accepted at login (see
    ``app.auth.dependencies``) can create the membership while this request sits
    pending. Approving then is still meaningful: it resolves the queue entry and
    records who decided it.
    """
    _require_active(space)

    request = session.execute(
        select(SpaceAccessRequest)
        .where(
            SpaceAccessRequest.id == request_id,
            SpaceAccessRequest.space_id == space.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    # Scoped to this Space, so an id belonging to another Space's queue reads as
    # "no such request" rather than acting on a neighbouring tenant's row.
    if request is None:
        raise AccessRequestNotFoundError(request_id)

    if request.status is not AccessRequestStatus.PENDING:
        raise AccessRequestAlreadyDecidedError(request_id)

    request.status = AccessRequestStatus.APPROVED if approve else AccessRequestStatus.DENIED
    request.decided_at = utcnow()
    request.decided_by_user_id = decider.id

    if approve and _load_membership(session, space.id, request.user_id) is None:
        session.add(
            SpaceMembership(
                space_id=space.id,
                user_id=request.user_id,
                role=MembershipRole.MEMBER,
            )
        )

    session.commit()

    requester = session.execute(select(User).where(User.id == request.user_id)).scalar_one()
    return request, requester


def _email_belongs_to_a_member(session: Session, space_id: int, email: str) -> bool:
    """Is any user holding this address already inside this Space?

    The join goes through ``users`` because an invitation is addressed to an
    email while a membership is held by a ``user_id``, and the two only meet
    there. ``func.lower`` on the stored side is not redundant with the invitation
    table's lowercase CHECK: this reads ``users.email``, which carries no such
    constraint — it is written from whatever casing the Auth0 token supplied.

    Returns true if *any* matching user is a member. An address can map to
    several ``users`` rows by design (a database signup and a Google login of the
    same address are separate ``sub`` values), and if any one of them is already
    in the Space then inviting the address again would be inviting somebody who
    is demonstrably already here.
    """
    return (
        session.execute(
            select(SpaceMembership.id)
            .join(User, User.id == SpaceMembership.user_id)
            .where(
                SpaceMembership.space_id == space_id,
                func.lower(User.email) == email,
            )
        ).first()
        is not None
    )


def _pending_invitation_id(session: Session, space_id: int, email: str) -> Optional[int]:
    return session.execute(
        select(SpaceInvitation.id).where(
            SpaceInvitation.space_id == space_id,
            func.lower(SpaceInvitation.email) == email,
            SpaceInvitation.status == InvitationStatus.PENDING,
        )
    ).scalar_one_or_none()


def create_invitation(
    session: Session,
    space: Space,
    inviter: User,
    *,
    email: str,
    role: MembershipRole,
    inviter_role: MembershipRole,
) -> SpaceInvitation:
    """Pre-approve an address for this Space at a given role.

    ``email`` is expected already lowercased and stripped — ``InvitationCreate``
    does that at the edge, and the table's CHECK constraint is the backstop.

    Four things are refused. An **archived** Space takes no new members at all.
    An address that already belongs to a member has nothing to be invited to, and
    creating the row anyway would leave a permanently unclaimable invitation in
    the admin's list. A second **pending** invitation would duplicate the first
    with no effect, since the first already admits them. And a role above the
    inviter's own is escalation rather than delegation — see
    :class:`InvitationRoleTooHighError`.

    A **revoked or accepted** invitation for the same address is deliberately not
    among the refusals: the partial unique index constrains pending rows only,
    exactly as with access requests, so an address invited and revoked in March
    can be invited again in June, and a member who was removed can be invited
    back.

    No email is sent. The invitation is a row saying "this address is welcome";
    the inviter shares the Space's ordinary link out of band, and the row is
    claimed at login by ``app.auth.dependencies._claim_pending_invitations`` on
    proof of a *verified* address. That gate is what makes an address-keyed
    pre-approval safe, and nothing here should be read as trusting the address
    itself.
    """
    _require_active(space)

    if not role_at_least(inviter_role, role):
        raise InvitationRoleTooHighError(role)

    if _email_belongs_to_a_member(session, space.id, email):
        raise InvitedUserAlreadyMemberError(email)

    if _pending_invitation_id(session, space.id, email) is not None:
        raise DuplicatePendingInvitationError(email)

    invitation = SpaceInvitation(
        space_id=space.id,
        email=email,
        role=role,
        invited_by_user_id=inviter.id,
    )
    session.add(invitation)
    try:
        session.commit()
    except IntegrityError:
        # Two admins inviting the same address at once: both pass the check
        # above before either commits, and the partial unique index is what
        # actually decides it. Converting the violation into the same 409 the
        # slower path returns keeps a race from surfacing as a 500.
        session.rollback()
        raise DuplicatePendingInvitationError(email)

    return invitation


def list_invitations(
    session: Session, space: Space, *, status: Optional[InvitationStatus]
) -> Sequence[SpaceInvitation]:
    """This Space's invitations, oldest first, optionally filtered by status.

    Unfiltered by default, matching :func:`list_access_requests`: accepted and
    revoked rows are retained as history precisely so an admin can see that an
    address was invited before, and hiding them by default would waste the
    retention the schema pays for.
    """
    query = (
        select(SpaceInvitation)
        .where(SpaceInvitation.space_id == space.id)
        .order_by(SpaceInvitation.created_at, SpaceInvitation.id)
    )
    if status is not None:
        query = query.where(SpaceInvitation.status == status)

    return session.execute(query).scalars().all()


def revoke_invitation(session: Session, space: Space, *, invitation_id: int) -> SpaceInvitation:
    """Withdraw a pending invitation.

    A **status transition, not a delete.** The row is the record that this
    address was invited and by whom, and an admin asking "who invited
    someone@rival.example?" after the fact is exactly the question the retention
    exists to answer — a ``DELETE`` would erase the evidence along with the
    access.

    Only a *pending* invitation can be revoked. An accepted one is refused rather
    than silently succeeding, because revoking it would not remove the membership
    it already produced: the admin would be told the access was withdrawn while
    the person remained in the Space. An already-revoked one is refused for the
    same reason it is refused for a re-archive — the caller believes they are
    ending something live, and a success would hide that somebody else got there
    first.

    ``with_for_update`` closes the race between a revoke and a login claiming the
    same row. Without it, two transactions could both read the invitation as
    pending and one would overwrite the other's transition, leaving an invitation
    marked ``revoked`` whose membership had already been created.
    """
    _require_active(space)

    invitation = session.execute(
        select(SpaceInvitation)
        .where(
            SpaceInvitation.id == invitation_id,
            SpaceInvitation.space_id == space.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()

    # Scoped to this Space, so an id from another Space's list reads as "no such
    # invitation" rather than acting on a neighbouring tenant's row.
    if invitation is None:
        raise InvitationNotFoundError(invitation_id)

    if invitation.status is not InvitationStatus.PENDING:
        raise InvitationAlreadyResolvedError(invitation_id)

    invitation.status = InvitationStatus.REVOKED
    session.commit()
    return invitation


def preview_status(session: Session, space: Space, user: User) -> PreviewStatus:
    """Where this caller stands with this Space, for the cold link-holder view."""
    membership = session.execute(
        select(SpaceMembership.id).where(
            SpaceMembership.space_id == space.id,
            SpaceMembership.user_id == user.id,
        )
    ).scalar_one_or_none()
    if membership is not None:
        return "member"

    latest = (
        session.execute(
            select(SpaceAccessRequest.status)
            .where(
                SpaceAccessRequest.space_id == space.id,
                SpaceAccessRequest.user_id == user.id,
            )
            .order_by(SpaceAccessRequest.created_at.desc(), SpaceAccessRequest.id.desc())
        )
        .scalars()
        .first()
    )

    if latest is AccessRequestStatus.PENDING:
        return "pending"
    if latest is AccessRequestStatus.DENIED:
        return "denied"
    # An approved request with no membership means the membership was removed
    # afterwards. Reporting "none" rather than "member" is both truthful and
    # useful: it lets them ask again, which "member" would not.
    return "none"
