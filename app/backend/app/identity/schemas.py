"""Pydantic schemas for the Space HTTP API.

Kept separate from ``app.identity.models`` for the same reason ``app.schemas`` is
kept separate from ``app.db.models``: those describe how a Space is stored, these
describe what crosses the wire.

**The integer ``Space.id`` appears in none of these.** It is a sequential
primary key and therefore enumerable, whereas ``public_id`` is a 128-bit random
token that *is* the capability granting access to a Space. Leaking the integer
anywhere — a response body, an error message, a ``Location`` header — would hand
an attacker a way to reason about Spaces they were never given a link to. The
protection is structural rather than a matter of remembering: none of the models
below declares an ``id`` field, so Pydantic will not serialise one even when it
is handed an ORM object that has it.

``users.id`` *is* exposed, as ``user_id``, and ``resources.id`` as ``id`` on
:class:`ResourceRead`. That is a different judgement, not an oversight: both are
only ever visible to people already inside the Space, and their routes are
addressed by those ids, so they have to cross the wire. What makes ``Space.id``
different is that it is the one integer an *outsider* could reason about — the
capability is the ``public_id`` link, and admission is Space-level, so a Resource
is never reachable without first being a member of its Space and its sequential
id discloses nothing to anyone who is not already there.
"""

from datetime import datetime, time
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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

# The four states a link-holder can be in with respect to a Space, as reported by
# ``GET /spaces/{public_id}/preview``.
PreviewStatus = Literal["none", "pending", "denied", "member"]

_NAME_MAX = 200
_DESCRIPTION_MAX = 4000
_MESSAGE_MAX = 1000
# A slot cannot be longer than the day it sits in. This is a sanity bound on the
# stored column, not the operating-hours model: resolving these local wall-clock
# values against the Space's zone to a bookable UTC window is a boundary concern,
# owned where booking evaluation happens and not here.
_SLOT_MINUTES_MAX = 1440
# Sanity bounds on the rule-engine parameters, not values the engine itself
# enforces — that is task 4.13b's job. These only keep an obviously-wrong value
# (a negative cap, a duration measured in years) out of the database.
_MAX_DURATION_MINUTES_MAX = 7 * 1440  # a week, in minutes
_BOOKING_HORIZON_DAYS_MAX = 3650  # ten years
_MAX_BOOKINGS_PER_WEEK_MAX = 1000
_MAX_BOOKINGS_PER_MONTH_MAX = 1000
# Matches ``users.email`` and ``space_invitations.email``, both String(320) — the
# maximum length RFC 5321 permits for a full address.
_EMAIL_MAX = 320


class SpaceCreate(BaseModel):
    """The body of ``POST /spaces``.

    Carries no operating-hours or rule-parameter fields — a fresh Space gets
    sensible default hours and slot interval (see ``service.create_space``) so
    it is immediately bookable, and the four rule parameters default to unset.
    Setting either is a follow-up ``PATCH`` through :class:`SpaceUpdate`, not a
    creation-time concern.
    """

    name: str = Field(min_length=1, max_length=_NAME_MAX)
    description: Optional[str] = Field(default=None, max_length=_DESCRIPTION_MAX)


class SpaceUpdate(BaseModel):
    """The body of ``PATCH /spaces/{public_id}``.

    All fields are optional, and *omitted* is deliberately distinct from
    *explicitly null*: omitting ``description`` leaves it alone, while sending
    ``null`` clears it. The router reads ``model_fields_set`` to tell the two
    apart, which is the only way to express "clear this field" in a PATCH without
    inventing a sentinel value.

    ``name`` and ``timezone`` are the exception — neither is nullable in the
    database, so an explicit null for either is a client error rather than an
    instruction, and is rejected here as 422 instead of reaching the database as
    an ``IntegrityError`` 500.

    ``opens_at`` / ``closes_at`` / ``slot_minutes`` and the four rule parameters
    are all nullable, following the same omit-vs-null convention: an explicit
    null clears the column back to "no restriction" / "rule not enforced", and
    an omitted field is left as it was.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=_NAME_MAX)
    description: Optional[str] = Field(default=None, max_length=_DESCRIPTION_MAX)
    timezone: Optional[str] = Field(default=None, min_length=1, max_length=64)
    opens_at: Optional[time] = None
    closes_at: Optional[time] = None
    slot_minutes: Optional[int] = Field(default=None, gt=0, le=_SLOT_MINUTES_MAX)
    max_duration_minutes: Optional[int] = Field(default=None, gt=0, le=_MAX_DURATION_MINUTES_MAX)
    booking_horizon_days: Optional[int] = Field(default=None, gt=0, le=_BOOKING_HORIZON_DAYS_MAX)
    max_bookings_per_week: Optional[int] = Field(default=None, gt=0, le=_MAX_BOOKINGS_PER_WEEK_MAX)
    max_bookings_per_month: Optional[int] = Field(
        default=None, gt=0, le=_MAX_BOOKINGS_PER_MONTH_MAX
    )

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"{value!r} is not a known IANA timezone name")
        return value

    @model_validator(mode="after")
    def _reject_explicit_null_name(self) -> "SpaceUpdate":
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name may not be null; omit it to leave it unchanged")
        if "timezone" in self.model_fields_set and self.timezone is None:
            raise ValueError("timezone may not be null; omit it to leave it unchanged")
        return self


class SpaceRead(BaseModel):
    """A Space as seen from inside — the response for members only.

    ``my_role`` travels with the Space so the frontend can decide which controls
    to render without a second round trip. It is a convenience, never a security
    boundary: every privileged route re-checks the role server-side.

    ``opens_at`` / ``closes_at`` / ``slot_minutes`` are this Space's operating
    hours and slot interval — every Resource in it shares them, since a Resource
    carries no configuration of its own. The four rule-parameter fields are the
    canon's per-Space limits; ``null`` means that rule is not enforced here.

    All seven of those fields are, since task 6.6, **derived from this
    Space's ``space_rules`` rows** rather than read off columns on ``Space``
    itself — see ``app.identity.service.space_schedule_fields``, which
    ``build`` below takes as its ``schedule`` argument rather than deriving
    it again here. This model's own shape is unchanged: the storage moved,
    the wire contract did not, which is the whole point of task 6.6's
    write-through shim.
    """

    model_config = ConfigDict(from_attributes=True)

    public_id: str
    name: str
    description: Optional[str]
    timezone: str
    opens_at: Optional[time]
    closes_at: Optional[time]
    slot_minutes: Optional[int]
    max_duration_minutes: Optional[int]
    booking_horizon_days: Optional[int]
    max_bookings_per_week: Optional[int]
    max_bookings_per_month: Optional[int]
    created_at: datetime
    archived_at: Optional[datetime]
    my_role: MembershipRole

    @classmethod
    def build(cls, space: Space, role: MembershipRole, schedule: dict) -> "SpaceRead":
        """``schedule`` is ``app.identity.service.space_schedule_fields(session,
        space)`` — the one place the seven fields below are derived from
        ``space_rules`` rows, called once per ``Space`` and reused here so
        every route serving a ``SpaceRead`` agrees on how they are read.
        """
        return cls(
            public_id=space.public_id,
            name=space.name,
            description=space.description,
            timezone=space.timezone,
            opens_at=schedule["opens_at"],
            closes_at=schedule["closes_at"],
            slot_minutes=schedule["slot_minutes"],
            max_duration_minutes=schedule["max_duration_minutes"],
            booking_horizon_days=schedule["booking_horizon_days"],
            max_bookings_per_week=schedule["max_bookings_per_week"],
            max_bookings_per_month=schedule["max_bookings_per_month"],
            created_at=space.created_at,
            archived_at=space.archived_at,
            my_role=role,
        )


class SpacePreview(BaseModel):
    """The cold link-holder view: enough to decide whether to ask to come in.

    Deliberately thin. Anyone holding the link can fetch this without being a
    member, so everything here is disclosed to whoever the link was forwarded to.
    That rules out the member list, member counts, and any hint of the bookings
    inside — a count alone would tell an outsider how busy a private resource is,
    and a member list would leak who uses it.
    """

    public_id: str
    name: str
    description: Optional[str]
    status: PreviewStatus


class ResourceCreate(BaseModel):
    """The body of ``POST /spaces/{public_id}/resources``.

    ``name`` only. A Resource is one of N indistinguishable courts and carries
    no configuration of its own — operating hours, slot interval, and every rule
    limit live on the Space, not here.
    """

    name: str = Field(min_length=1, max_length=_NAME_MAX)


class ResourceUpdate(BaseModel):
    """The body of ``PATCH /spaces/{public_id}/resources/{resource_id}``.

    ``name`` only, and an explicit null is a client error rejected here as 422
    rather than left to surface as an ``IntegrityError`` 500 — ``name`` is
    ``NOT NULL`` in the database.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=_NAME_MAX)

    @model_validator(mode="after")
    def _reject_explicit_null_name(self) -> "ResourceUpdate":
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name may not be null; omit it to leave it unchanged")
        return self


class ResourceRead(BaseModel):
    """A Resource as seen from inside its Space.

    ``id`` is the integer primary key and it is exposed deliberately: a Resource
    carries no ``public_id`` because admission is Space-level, so this id is the
    handle its own routes are addressed by, and it is only ever visible to people
    already inside the Space. That is the same judgement made for ``user_id`` and
    the opposite of the one made for ``Space.id`` — see this module's docstring.

    No hours, no slot interval, no rule parameters: a Resource is a unit of
    bookable capacity, config-free by design. See :class:`SpaceRead` for where
    that configuration actually lives.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: datetime
    archived_at: Optional[datetime]

    @classmethod
    def build(cls, resource: Resource) -> "ResourceRead":
        return cls.model_validate(resource)


class MemberRead(BaseModel):
    """One membership, as shown to people already inside the Space."""

    user_id: int
    email: str
    name: Optional[str]
    role: MembershipRole
    created_at: datetime

    @classmethod
    def build(cls, membership: SpaceMembership, user: User) -> "MemberRead":
        return cls(
            user_id=user.id,
            email=user.email,
            name=user.name,
            role=membership.role,
            created_at=membership.created_at,
        )


class MembershipUpdate(BaseModel):
    """The body of ``PATCH /spaces/{public_id}/members/{user_id}``."""

    role: MembershipRole


class AccessRequestCreate(BaseModel):
    """The body of ``POST /spaces/{public_id}/access-requests``.

    A message is optional and free text — "I'm on the Tuesday team" — because the
    admin deciding has otherwise only an email address to go on, and an email
    address is not much on which to let someone into a private Space.
    """

    message: Optional[str] = Field(default=None, max_length=_MESSAGE_MAX)


class AccessRequestRead(BaseModel):
    """One access request, as shown to the admins reviewing the queue.

    The requester's ``email`` and ``name`` are joined in rather than left as a
    bare ``user_id``: this is the one screen where an admin decides whether a
    stranger gets into their Space, and a numeric id gives them nothing to decide
    on. Only admin+ ever sees this model — the requester's own view of their
    standing is the single ``status`` word in ``SpacePreview``.
    """

    id: int
    user_id: int
    email: str
    name: Optional[str]
    status: AccessRequestStatus
    message: Optional[str]
    created_at: datetime
    decided_at: Optional[datetime]
    decided_by_user_id: Optional[int]

    @classmethod
    def build(cls, request: SpaceAccessRequest, user: User) -> "AccessRequestRead":
        return cls(
            id=request.id,
            user_id=user.id,
            email=user.email,
            name=user.name,
            status=request.status,
            message=request.message,
            created_at=request.created_at,
            decided_at=request.decided_at,
            decided_by_user_id=request.decided_by_user_id,
        )


class InvitationCreate(BaseModel):
    """The body of ``POST /spaces/{public_id}/invitations``.

    **The email is lowercased here, at the edge**, rather than in the service or
    the router. ``space_invitations.email`` carries a
    ``CHECK (email = lower(email))``, and the login-time claim in
    ``app.auth.dependencies`` matches on the lowercased address, so an
    un-normalised value would either be rejected by the database as a 500 or —
    worse, had the constraint not existed — stored as ``Alice@Example.com`` and
    silently never matched when Alice logged in. Normalising once, where the
    value enters the system, is what makes "stored lowercased" a property of the
    data rather than a habit of whichever caller wrote it.

    Surrounding whitespace is stripped for the same reason: an address pasted out
    of an email client routinely arrives with a trailing space, and a stored
    ``"alice@example.com "`` would never match a token's ``alice@example.com``.

    The shape check is deliberately minimal — one ``@`` with something either
    side, no internal whitespace. It is here to catch a transposed field or an
    obviously empty value, not to adjudicate RFC 5322; ``pydantic[email]`` would
    need a new dependency, and the real proof that an address is deliverable and
    belongs to its holder is Auth0's ``email_verified`` claim at login, which is
    what actually gates the invitation being claimed.
    """

    email: str = Field(min_length=3, max_length=_EMAIL_MAX)
    role: MembershipRole = MembershipRole.MEMBER

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        email = value.strip().lower()

        local, separator, domain = email.partition("@")
        if not separator or not local or not domain or "@" in domain:
            raise ValueError("email must be a single address of the form name@domain")
        if any(character.isspace() for character in email):
            raise ValueError("email may not contain whitespace")

        return email


class InvitationRead(BaseModel):
    """One invitation, as shown to the admins managing the Space.

    Only admin+ ever sees this model. That matters because it lists the email
    addresses of people who are *not* members — an invitation to
    ``someone@rival.example`` is visible here before they have accepted anything,
    and exposing it to ordinary members would disclose who is being recruited
    into the Space.

    There is no ``invitation link`` field because there is no per-invitation
    token: the invitee is admitted by the address on their verified token, so the
    thing the inviter shares is the Space's ordinary ``public_id`` link. A
    per-invitation secret would be a second capability to leak, and it would
    admit whoever held it rather than whoever owns the address.
    """

    id: int
    email: str
    role: MembershipRole
    status: InvitationStatus
    invited_by_user_id: int
    created_at: datetime
    accepted_at: Optional[datetime]

    @classmethod
    def build(cls, invitation: SpaceInvitation) -> "InvitationRead":
        return cls(
            id=invitation.id,
            email=invitation.email,
            role=invitation.role,
            status=invitation.status,
            invited_by_user_id=invitation.invited_by_user_id,
            created_at=invitation.created_at,
            accepted_at=invitation.accepted_at,
        )
