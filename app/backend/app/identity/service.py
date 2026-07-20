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

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import utcnow
from app.identity.models import (
    AccessRequestStatus,
    MembershipRole,
    Space,
    SpaceAccessRequest,
    SpaceMembership,
    User,
)
from app.identity.schemas import PreviewStatus, SpaceUpdate


class SpaceArchivedError(Exception):
    """A mutation was attempted on a Space that has been archived."""


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
    """Create a Space and make its creator the owner, atomically.

    The two writes share one transaction because a Space with no owner is
    unrecoverable: nobody could archive it, manage it, or be added to it. If the
    membership insert fails, the Space must not survive it.
    """
    space = Space(name=name, description=description, created_by_user_id=creator.id)
    session.add(space)
    session.flush()

    session.add(SpaceMembership(space_id=space.id, user_id=creator.id, role=MembershipRole.OWNER))
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
