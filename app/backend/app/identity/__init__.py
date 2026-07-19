"""Stream 2's identity and access package: users, Spaces, and who may enter one.

The models are re-exported here so that importing the package is enough to
register them on the shared ``Base.metadata``. Alembic's ``env.py`` relies on
that: a model class that is never imported is invisible to autogenerate, and the
failure mode is a silently empty migration rather than an error.
"""

from app.identity.models import (
    AccessRequestStatus,
    InvitationStatus,
    MembershipRole,
    Space,
    SpaceAccessRequest,
    SpaceInvitation,
    SpaceMembership,
    User,
    generate_public_id,
)

__all__ = [
    "AccessRequestStatus",
    "InvitationStatus",
    "MembershipRole",
    "Space",
    "SpaceAccessRequest",
    "SpaceInvitation",
    "SpaceMembership",
    "User",
    "generate_public_id",
]
