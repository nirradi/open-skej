"""SQLAlchemy models for Stream 2's identity and access schema.

These are the tables Stream 2 owns: who a user is, what Spaces exist, and who is
allowed into them. Booking mechanics stay entirely in Stream 1's
``app/db/models.py``; the two only ever meet in Stream 4, which turns
``bookings.resource_id`` and ``bookings.user_id`` into real foreign keys onto
``spaces.id`` and ``users.id``.

``Base``, ``UtcDateTime`` and ``utcnow`` are imported from ``app.db.models``
rather than redefined. One declarative base means one metadata registry, which is
what lets Stream 4 write those foreign keys without a cross-base reference. The
resulting risk — that autogenerate would try to claim Stream 1's ``bookings``
table — is handled mechanically by ``app.migration_filter``.

Design notes that apply throughout:

* **Enums use ``native_enum=False`` with ``create_constraint=True``**, matching
  the ``BookingStatus`` precedent. The values land as plain strings backed by a
  ``CHECK``, so there is no Postgres ``TYPE`` to ``ALTER`` when a role or status
  is added later — an in-place enum change is one of the more painful migrations
  to write, and a CHECK constraint swap is not. It also keeps the partial-index
  predicates below (``WHERE status = 'pending'``) as ordinary string comparisons.
* **Nothing is ever deleted.** A Space ends at ``archived_at``; an access request
  and an invitation both keep their decided rows as history. Consequently no
  foreign key here carries ``ON DELETE CASCADE``: there is no delete to cascade,
  and a cascade would quietly destroy the audit trail if one were ever added.
* **Timestamps use ``UtcDateTime``**, which rejects naive datetimes outright, so
  a local time cannot silently be stored as if it were UTC.
"""

import enum
import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base, UtcDateTime, utcnow

# Number of random bytes behind a Space's ``public_id``. 16 bytes is 128 bits of
# entropy, rendered by ``secrets.token_urlsafe`` as 22 URL-safe characters. The
# link *is* the capability that grants access to a Space, so this has to be
# infeasible to guess or enumerate; 128 bits is the same margin a UUIDv4 offers.
_PUBLIC_ID_BYTES = 16


def generate_public_id() -> str:
    """A fresh unguessable Space identifier.

    ``secrets`` rather than ``random``: the latter is a Mersenne Twister seeded
    from predictable state, and observing a handful of its outputs is enough to
    reconstruct the sequence — which for a capability URL would mean an attacker
    could derive every other Space's link from one they were legitimately given.
    """
    return secrets.token_urlsafe(_PUBLIC_ID_BYTES)


class MembershipRole(str, enum.Enum):
    """A user's authority within one Space.

    Scoped per Space rather than globally: there is no superuser, so two tenants
    on the same deployment stay genuinely independent. ``owner`` may archive the
    Space, ``admin`` may manage members, invitations and access requests, and
    ``member`` may only book.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class AccessRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class InvitationStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"


def _string_enum(enum_cls: type[enum.Enum], name: str, length: int) -> Enum:
    """An ``Enum`` column type stored as its string value with a CHECK behind it.

    ``values_callable`` is what makes the stored value ``"owner"`` rather than
    the member *name* ``"OWNER"`` — without it the partial-index predicates and
    check constraints in this module, which are written against the lowercase
    values, would silently never match anything.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


_ROLE_TYPE = _string_enum(MembershipRole, "membership_role", 16)
_ACCESS_REQUEST_STATUS_TYPE = _string_enum(AccessRequestStatus, "access_request_status", 16)
_INVITATION_STATUS_TYPE = _string_enum(InvitationStatus, "invitation_status", 16)


class User(Base):
    """A person, provisioned just-in-time on their first authenticated request.

    ``auth0_sub`` is the join key to Auth0 and the only stable identifier here.
    Email is deliberately *not* unique: Auth0 lets the same address arrive under
    two different ``sub`` values (a database signup and a Google login are
    separate identities unless account linking is configured), and a unique
    constraint on email would turn that ordinary situation into a hard login
    failure. Uniqueness therefore lives on ``auth0_sub`` alone, and email is
    treated as mutable — refreshed from the token on every login.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    auth0_sub: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[Optional[str]] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, auth0_sub={self.auth0_sub!r}, email={self.email!r})"


class Space(Base):
    """One bookable thing — a court, a room, a piece of equipment.

    Spaces are **not discoverable**: there is no endpoint that lists them all, so
    the only way to reach one you are not already a member of is to be handed its
    ``public_id`` link. That makes ``public_id`` a bearer capability, which is why
    it is a random token and why the integer ``id`` — sequential, and therefore
    enumerable — is never exposed over the API.

    ``archived_at`` is the sole end-state. Deleting a Space would have to decide
    what happens to bookings already made against it, and bookings belong to
    Stream 1, so that question is left to Stream 4 rather than pre-empted with a
    ``DELETE`` here.
    """

    __tablename__ = "spaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, default=generate_public_id)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    archived_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        # Guards against a caller passing an explicit short public_id and
        # quietly reducing a 128-bit capability to something brute-forceable.
        # The generator always produces 22 characters.
        CheckConstraint("length(public_id) >= 20", name="ck_spaces_public_id_length"),
    )

    def __repr__(self) -> str:
        return f"Space(id={self.id!r}, public_id={self.public_id!r}, name={self.name!r})"


class SpaceMembership(Base):
    """The authorization record: this user, in this Space, at this role.

    This table — not Auth0 — is the source of truth for permissions. Auth0 proves
    *identity*; per-Space roles stored in Auth0 would mean a Management API round
    trip on every membership change, and an outage there would become an outage
    here.
    """

    __tablename__ = "space_memberships"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    role: Mapped[MembershipRole] = mapped_column(_ROLE_TYPE, default=MembershipRole.MEMBER)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    __table_args__ = (
        # One membership per user per Space. Without this, approving the same
        # access request twice — or a race between two admins — would leave a
        # user holding two rows at different roles, and every permission check
        # would then depend on which row it happened to read first.
        Index("uq_space_memberships_space_user", "space_id", "user_id", unique=True),
        # "Which Spaces do I belong to?" — the GET /spaces query — filters on
        # user_id alone, which the composite index above cannot serve because
        # user_id is not its leading column.
        Index("ix_space_memberships_user", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"SpaceMembership(space_id={self.space_id!r},"
            f" user_id={self.user_id!r}, role={self.role.value!r})"
        )


class SpaceAccessRequest(Base):
    """A cold link-holder asking to be let into a Space.

    Decided rows are kept rather than deleted, so an admin can see that a user
    was denied last month before approving them today. That retention is exactly
    what rules out a plain ``UNIQUE (space_id, user_id)``: it would permit only
    one request ever, so a user denied once could never ask again.

    The partial index below is the precise constraint instead — at most one
    *pending* request per user per Space, with any number of decided ones
    alongside it. Postgres is the only backend Stream 2 targets, so the
    ``postgresql_where`` predicate is not a portability compromise.
    """

    __tablename__ = "space_access_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[AccessRequestStatus] = mapped_column(
        _ACCESS_REQUEST_STATUS_TYPE, default=AccessRequestStatus.PENDING
    )
    message: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)
    decided_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), default=None)

    __table_args__ = (
        Index(
            "uq_space_access_requests_pending",
            "space_id",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # The admin review queue reads one Space's requests filtered by status.
        Index("ix_space_access_requests_space_status", "space_id", "status"),
        # A decided request must record when and by whom; a pending one must
        # record neither. Task 2.6 approves a request and creates the membership
        # in one transaction, and this constraint is what stops a half-applied
        # decision — status flipped, decider unrecorded — from persisting.
        CheckConstraint(
            "(status = 'pending' AND decided_at IS NULL AND decided_by_user_id IS NULL)"
            " OR (status IN ('approved', 'denied')"
            " AND decided_at IS NOT NULL AND decided_by_user_id IS NOT NULL)",
            name="ck_space_access_requests_decision_complete",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"SpaceAccessRequest(id={self.id!r}, space_id={self.space_id!r},"
            f" user_id={self.user_id!r}, status={self.status.value!r})"
        )


class SpaceInvitation(Base):
    """A pre-approval, addressed to an email that may not have an account yet.

    An invitation is keyed on email rather than ``user_id`` precisely because the
    invitee usually does not exist in ``users`` at the time it is written — the
    row is claimed on their first login, when the JWT finally supplies a
    verified address. That claim is a lookup by email, so the address is stored
    lowercased and the check constraint below enforces it: matching
    case-insensitively at query time instead would mean either a
    ``lower(email)`` scan or a silently missed invitation for
    ``Alice@Example.com``.

    Revoked and accepted rows are retained, so — as with access requests — the
    uniqueness that matters is over pending rows only: an address that was
    invited and revoked can be invited again.
    """

    __tablename__ = "space_invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[MembershipRole] = mapped_column(_ROLE_TYPE, default=MembershipRole.MEMBER)
    status: Mapped[InvitationStatus] = mapped_column(
        _INVITATION_STATUS_TYPE, default=InvitationStatus.PENDING
    )
    invited_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        Index(
            "uq_space_invitations_pending",
            "space_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # The login-time claim in task 2.3 asks "are there pending invitations
        # for this address?" across all Spaces, so email leads the index.
        Index("ix_space_invitations_email_status", "email", "status"),
        CheckConstraint("email = lower(email)", name="ck_space_invitations_email_lowercase"),
        CheckConstraint(
            "(status = 'accepted' AND accepted_at IS NOT NULL)"
            " OR (status IN ('pending', 'revoked') AND accepted_at IS NULL)",
            name="ck_space_invitations_accepted_at_matches_status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"SpaceInvitation(id={self.id!r}, space_id={self.space_id!r},"
            f" email={self.email!r}, status={self.status.value!r})"
        )
