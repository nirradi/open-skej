"""SQLAlchemy models for Stream 2's identity and access schema.

These are the identity and access tables: who a user is, what Spaces exist, what
Resources those Spaces hold, and who is allowed into them. Booking mechanics stay
in ``app/db/models.py``, but ``bookings.resource_id`` and ``bookings.user_id`` are
real foreign keys onto ``resources.id`` and ``users.id`` — both defined here — so
a booking is against a real Resource, made by a real user.

``Base``, ``UtcDateTime`` and ``utcnow`` are imported from ``app.db.models``
rather than redefined. One declarative base means one metadata registry, which is
what lets those foreign keys be written without a cross-base reference — and, now
that the booking store is folded into Alembic, what lets a single migration
history own the whole schema with no table-scoping filter.

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
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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
    """A venue holding many Resources — a club, a building, a shared lab.

    A Space is *not* itself the thing booked: it is the tenant boundary and the
    unit of admission. The bookable calendars are its :class:`Resource` rows (a
    club has two tennis courts; a lab has three instruments), and a member of the
    Space may book any of them. Membership, roles, invitations and access
    requests are all Space-level for exactly that reason — you are admitted to the
    venue, not to one court — which is what keeps the whole authorization model
    unchanged by the venue/Resource split.

    Spaces are **not discoverable**: there is no endpoint that lists them all, so
    the only way to reach one you are not already a member of is to be handed its
    ``public_id`` link. That makes ``public_id`` a bearer capability, which is why
    it is a random token and why the integer ``id`` — sequential, and therefore
    enumerable — is never exposed over the API. ``public_id`` lives on the Space
    and not on a Resource because admission is Space-level; a Resource is reachable
    only once you are already inside the venue and needs no unguessable id.

    ``timezone`` is the venue's IANA zone (``Europe/Berlin``, never a fixed
    offset). It lives here because a venue is in one physical place, and it
    exists to resolve this Space's own *operating hours* — local wall-clock
    config — to a UTC instant per date. Stored instants everywhere else carry no
    zone; this is the one place a zone is a property of the data, because
    operating hours are a rule that lands on a different UTC moment as the
    calendar and DST move. An offset column would be the version of this that
    looks right in July and is wrong in January. It is the one genuinely
    per-Space column left on this table: operating hours, slot interval, and
    every rule-engine limit are configured instead as rows on
    :class:`SpaceRule` below, one row per *instance* of a registered rule
    type, which is what lets a Space hold any number of instances of a type
    and scope each one with ``applies_to`` rather than one fixed value for
    the whole venue.

    ``archived_at`` is the sole end-state. An archived Space rejects new bookings
    on any of its Resources; existing future bookings stay and remain cancellable.
    Deleting is never an option here — it would decide the fate of bookings made
    against the venue's Resources, and the audit trail is kept instead.
    """

    __tablename__ = "spaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, default=generate_public_id)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # An IANA zone name, defaulting to UTC. ``server_default`` so the column can
    # be added NOT NULL to a table that already holds rows, and so a Space created
    # by a path that does not set it still lands on a valid zone.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default=text("'UTC'"))
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


class SpaceRule(Base):
    """One configured instance of a rule type, governing one Space.

    Rule configuration lives here rather than as fixed columns on ``Space``,
    one row per *instance* of a rule type declared in
    ``rules.registry.REGISTRY`` — so a Space can hold any number of
    instances of a type ("Mon/Wed/Fri 10-15" and "Tue/Thu 8-12" as two
    ``availability_hours`` rows) instead of one fixed value for the whole
    venue.

    ``rule_type`` is the registry's **stable string id** (``availability_hours``,
    ``max_duration``, …) — never a Python class name, so renaming the class a
    type happens to be implemented by cannot silently orphan every row that
    named it. ``params`` is the JSONB blob ``RuleType.build`` validates and
    reads; its shape is entirely owned by the registered type, which is what
    keeps this table generic over a type registered after this migration
    ships.

    ``applies_to`` narrows *when* the instance is active, and is a new shape
    with nothing else in the codebase to reverse-engineer it from, so it is
    documented here. It carries **at most one facet** — the pair (a weekday
    set intersected with a date set) is reserved, not built: neither an
    intersecting nor a union reading of the two has an obviously right
    meaning and nobody has asked for it (``ops/plans/stream-6-plan.md``, "Out
    of scope"). The three legal shapes:

    * ``None`` — always applies. The shape every row this migration
      backfills is given, and the common case going forward.
    * ``{"weekdays": [0, 2, 4]}`` — a list of ``datetime.date.weekday()``
      integers, ``0`` = Monday, matching ``rules.Weekday``'s own numbering.
      The instance applies only when the booking's own **local** date (in
      the Space's zone — never UTC; see ``app.rules_stub._local_date``) has
      a weekday in this list.
    * ``{"dates": ["2026-12-25"]}`` — a list of ISO ``date`` strings
      (``date.isoformat()``). The instance applies only when the booking's
      local date is in this list.

    ``enabled`` is the pause switch, and pausing is the entire mechanism for
    "not today": a disabled row is never assembled into the canon, full
    stop. It is not the same operation as deleting the row — a paused rule
    instance is a decision an admin can reverse, and it says nothing about
    whether the row should still exist.

    No ``ON DELETE CASCADE`` on ``space_id``, matching every other foreign
    key onto ``spaces.id`` in this schema (``.claude/rules/identity-and-
    access.md``, "nothing is deleted") — a cascade would silently destroy a
    Space's configuration the moment the row referencing it went away, which
    is exactly the failure mode that rule protects against elsewhere. The row
    *itself*, unlike a booking or a membership, may be deleted outright: this
    table's "nothing is deleted" analogue is ``enabled``, not row survival —
    "nothing is deleted" is about admission history (who asked, who was let
    in), and a retired rule instance was never that.
    """

    __tablename__ = "space_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    rule_type: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # None means "always" — see the docstring above for the two narrower shapes.
    applies_to: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        # "Which rules govern this Space?" — every read this table serves —
        # filters on space_id alone.
        Index("ix_space_rules_space", "space_id"),
    )

    def __repr__(self) -> str:
        return (
            f"SpaceRule(id={self.id!r}, space_id={self.space_id!r},"
            f" rule_type={self.rule_type!r}, enabled={self.enabled!r})"
        )


class Resource(Base):
    """One of N indistinguishable courts inside a Space — a unit of bookable
    capacity, and nothing more.

    A Resource is what a booking is actually made against: ``bookings.resource_id``
    is a foreign key onto this table, and the overlap invariant is keyed on it, so
    two courts booked at the same hour do not collide while the same court twice
    does. That overlap constraint is the *only* thing that still distinguishes two
    Resources in the same Space — it belongs to exactly one :class:`Space` (its
    venue) and carries **no configuration and no permissions of its own**.
    Operating hours, slot interval, and every rule limit live on the Space; a
    member of the Space may book any Resource in it.

    A Resource has no ``public_id``. Admission is Space-level, so nothing reaches
    a Resource without first being inside its Space; there is no capability URL to
    protect and so no unguessable id to mint. Cross-tenant safety comes from
    resolving every Resource route through ``require_space_role`` on the parent
    Space, which returns 404 (never 403) for a Resource that exists but is not
    yours — the same oracle-free rule the Space routes already follow.

    ``archived_at`` retires a Resource without deleting it, matching the Space's
    own end-state; there is no delete and so no cascade.
    """

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    archived_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        # "Which Resources are in this Space?" — the Space page and the Resource
        # picker — filters on space_id alone.
        Index("ix_resources_space", "space_id"),
    )

    def __repr__(self) -> str:
        return f"Resource(id={self.id!r}, space_id={self.space_id!r}, name={self.name!r})"


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
