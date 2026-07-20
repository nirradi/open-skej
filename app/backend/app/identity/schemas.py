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

``users.id`` *is* exposed, as ``user_id``. That is a different judgement, not an
oversight: a member id is only ever visible to people already inside the Space,
and the membership routes are addressed by it, so it has to cross the wire.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.identity.models import MembershipRole, Space, SpaceMembership, User

# The four states a link-holder can be in with respect to a Space, as reported by
# ``GET /spaces/{public_id}/preview``.
PreviewStatus = Literal["none", "pending", "denied", "member"]

_NAME_MAX = 200
_DESCRIPTION_MAX = 4000


class SpaceCreate(BaseModel):
    """The body of ``POST /spaces``."""

    name: str = Field(min_length=1, max_length=_NAME_MAX)
    description: Optional[str] = Field(default=None, max_length=_DESCRIPTION_MAX)


class SpaceUpdate(BaseModel):
    """The body of ``PATCH /spaces/{public_id}``.

    Both fields are optional, and *omitted* is deliberately distinct from
    *explicitly null*: omitting ``description`` leaves it alone, while sending
    ``null`` clears it. The router reads ``model_fields_set`` to tell the two
    apart, which is the only way to express "clear this field" in a PATCH without
    inventing a sentinel value.

    ``name`` is the exception — it is not nullable in the database, so an
    explicit null is a client error rather than an instruction, and is rejected
    here as 422 instead of reaching the database as an ``IntegrityError`` 500.
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=_NAME_MAX)
    description: Optional[str] = Field(default=None, max_length=_DESCRIPTION_MAX)

    @model_validator(mode="after")
    def _reject_explicit_null_name(self) -> "SpaceUpdate":
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name may not be null; omit it to leave it unchanged")
        return self


class SpaceRead(BaseModel):
    """A Space as seen from inside — the response for members only.

    ``my_role`` travels with the Space so the frontend can decide which controls
    to render without a second round trip. It is a convenience, never a security
    boundary: every privileged route re-checks the role server-side.
    """

    model_config = ConfigDict(from_attributes=True)

    public_id: str
    name: str
    description: Optional[str]
    created_at: datetime
    archived_at: Optional[datetime]
    my_role: MembershipRole

    @classmethod
    def build(cls, space: Space, role: MembershipRole) -> "SpaceRead":
        return cls(
            public_id=space.public_id,
            name=space.name,
            description=space.description,
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
