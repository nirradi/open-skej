"""The authenticated-caller dependency: token in, ``User`` row out.

Two things happen on every authenticated request, and both are deliberate:

* **Just-in-time provisioning.** There is no signup endpoint and no Auth0
  webhook. The first time a token arrives bearing an unseen ``sub``, the ``users``
  row is created from the token's claims. This keeps Auth0 as the sole registry
  of credentials and leaves this database holding only what it needs to express
  authorization.
* **Invitation claiming**, gated on ``email_verified`` — see
  :func:`_claim_pending_invitations`, where the reasoning is spelled out, because
  it is the one place in Stream 2 where getting it wrong hands away access to a
  private Space.
"""

from typing import Any, Optional

from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.jwt import AuthError, TokenVerifier, get_token_verifier
from app.db.models import utcnow
from app.db.session import get_session
from app.identity.models import (
    InvitationStatus,
    SpaceInvitation,
    SpaceMembership,
    User,
)

_BEARER_PREFIX = "bearer"


def extract_bearer_token(authorization: Optional[str]) -> str:
    """Pull the credential out of an ``Authorization`` header value.

    Raises :class:`AuthError` rather than returning ``None`` so that a missing
    header and a malformed one travel the same path to 401. The scheme is
    compared case-insensitively because RFC 7235 defines it that way, and some
    clients send ``bearer``.
    """
    if not authorization:
        raise AuthError("Authorization header is missing")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != _BEARER_PREFIX:
        raise AuthError("Authorization header must be 'Bearer <token>'")

    return parts[1]


def _upsert_user(session: Session, claims: dict[str, Any]) -> User:
    """Find or create the ``users`` row for these verified claims.

    Keyed on ``sub``, never on email: ``users.email`` is intentionally
    non-unique, so an address does not identify a row. ``email`` and ``name`` are
    refreshed on every login because Auth0 is authoritative for both and a user
    who changes their address in Auth0 should not go on seeing the old one here.
    """
    sub = claims["sub"]
    email = (claims.get("email") or "").lower()
    name = claims.get("name")

    user = session.execute(select(User).where(User.auth0_sub == sub)).scalar_one_or_none()

    if user is None:
        user = User(auth0_sub=sub, email=email, name=name, last_login_at=utcnow())
        session.add(user)
        session.flush()
    else:
        user.email = email
        user.name = name
        user.last_login_at = utcnow()

    return user


def _claim_pending_invitations(session: Session, user: User, claims: dict[str, Any]) -> None:
    """Convert this user's pending invitations into memberships.

    **The ``email_verified`` gate is the whole point of this function.**

    ``users.email`` is not unique, and correctly so: Auth0 issues distinct ``sub``
    values for a database signup and a Google login of the same address, so a
    unique constraint would turn an ordinary second login into a hard failure.
    The consequence is that *an email address does not identify a person*.

    An invitation, however, is addressed to an email — it has to be, since the
    invitee usually has no account when it is written. So the only thing standing
    between an invitation and the wrong person is proof that the caller controls
    the address. Without this gate, anyone could sign up through the database
    connection using a victim's address, never confirm it, and inherit every
    Space that victim was invited to.

    A missing ``email_verified`` claim is treated as unverified. Defaulting the
    other way would mean any tenant misconfiguration that drops the claim
    silently opens the hole this check exists to close. An unverified caller
    simply keeps their pending invitations and can still use the ordinary
    access-request flow.
    """
    if claims.get("email_verified") is not True:
        return

    email = (claims.get("email") or "").lower()
    if not email:
        return

    # Emails are stored lowercased (enforced by a CHECK constraint), so this
    # compares like with like rather than relying on the caller's casing.
    pending = (
        session.execute(
            select(SpaceInvitation).where(
                func.lower(SpaceInvitation.email) == email,
                SpaceInvitation.status == InvitationStatus.PENDING,
            )
        )
        .scalars()
        .all()
    )

    for invitation in pending:
        already_member = session.execute(
            select(SpaceMembership.id).where(
                SpaceMembership.space_id == invitation.space_id,
                SpaceMembership.user_id == user.id,
            )
        ).scalar_one_or_none()

        if already_member is None:
            session.add(
                SpaceMembership(
                    space_id=invitation.space_id,
                    user_id=user.id,
                    role=invitation.role,
                )
            )

        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_at = utcnow()


def get_current_user(
    request: Request,
    session: Session = Depends(get_session),
    verifier: TokenVerifier = Depends(get_token_verifier),
) -> User:
    """The authenticated caller, provisioned on first sight.

    Any rejection raises :class:`AuthError`, which ``main.py`` maps to 401. The
    commit happens here rather than in the caller so that a handler which only
    reads still persists the login timestamp and any claimed invitations.
    """
    token = extract_bearer_token(request.headers.get("Authorization"))
    claims = verifier.verify(token)

    user = _upsert_user(session, claims)

    try:
        _claim_pending_invitations(session, user, claims)
        session.commit()
    except IntegrityError:
        # Two concurrent logins racing to claim the same invitation: the unique
        # index on (space_id, user_id) lets exactly one win. The loser's work is
        # already done by the winner, so rolling back and re-reading is correct
        # rather than an error worth surfacing.
        session.rollback()
        user = session.execute(select(User).where(User.auth0_sub == claims["sub"])).scalar_one()

    session.refresh(user)
    return user
