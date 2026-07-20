"""Tests for JIT user provisioning and invitation claiming.

Postgres-only — the partial unique indexes these rely on are Postgres features —
so the module skips when ``DATABASE_URL`` is unset, keeping Stream 1's SQLite
suite runnable standalone.

The centre of gravity here is :func:`test_an_unverified_email_claims_nothing`.
``users.email`` is deliberately non-unique, so an email address does not identify
a person; the ``email_verified`` gate is the only thing stopping an attacker from
signing up with a victim's address and inheriting the Spaces that victim was
invited to.
"""

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth.dependencies import _claim_pending_invitations, _upsert_user, extract_bearer_token
from app.auth.jwt import AuthError
from app.db.models import Base
from app.identity.models import (
    InvitationStatus,
    MembershipRole,
    SpaceInvitation,
    SpaceMembership,
    User,
)

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; these tests need `docker compose up -d`",
)


@pytest.fixture
def session():
    """A session on a schema built and torn down per test.

    Created directly from metadata rather than through Alembic: these tests are
    about behaviour, and rebuilding the schema per test keeps them independent of
    each other's rows and of migration ordering.
    """
    engine = create_engine(DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def _claims(**overrides):
    claims = {
        "sub": "auth0|invitee",
        "email": "invitee@example.com",
        "email_verified": True,
        "name": "Invitee",
    }
    claims.update(overrides)
    return claims


def _make_space_with_invitation(
    session: Session,
    *,
    email: str = "invitee@example.com",
    role: MembershipRole = MembershipRole.ADMIN,
    status: InvitationStatus = InvitationStatus.PENDING,
) -> tuple[int, SpaceInvitation]:
    """An inviter, a Space they own, and an invitation addressed to ``email``."""
    from app.identity.models import Space

    inviter = User(auth0_sub="auth0|inviter", email="owner@example.com")
    session.add(inviter)
    session.flush()

    space = Space(name="Centre Court", created_by_user_id=inviter.id)
    session.add(space)
    session.flush()

    invitation = SpaceInvitation(
        space_id=space.id,
        email=email.lower(),
        role=role,
        status=status,
        invited_by_user_id=inviter.id,
        accepted_at=datetime.now(timezone.utc) if status is InvitationStatus.ACCEPTED else None,
    )
    session.add(invitation)
    session.flush()

    return space.id, invitation


# --- The security gate. -----------------------------------------------------


def test_a_verified_email_claims_its_invitation(session: Session) -> None:
    """The positive case, without which every negative below would be vacuous."""
    space_id, invitation = _make_space_with_invitation(session)

    user = _upsert_user(session, _claims())
    _claim_pending_invitations(session, user, _claims())
    session.flush()

    membership = session.execute(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space_id,
            SpaceMembership.user_id == user.id,
        )
    ).scalar_one()

    assert membership.role is MembershipRole.ADMIN, "the invited role must be honoured"
    assert invitation.status is InvitationStatus.ACCEPTED
    assert invitation.accepted_at is not None


def test_an_unverified_email_claims_nothing(session: Session) -> None:
    """THE critical case: an unconfirmed address must inherit no access.

    Without this gate, an attacker signs up through the database connection using
    a victim's address, never confirms it, and silently joins every Space the
    victim was invited to. The invitation must survive untouched so the real
    owner of the address can still claim it later.
    """
    space_id, invitation = _make_space_with_invitation(session)

    claims = _claims(sub="auth0|impostor", email_verified=False)
    user = _upsert_user(session, claims)
    _claim_pending_invitations(session, user, claims)
    session.flush()

    membership = session.execute(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space_id,
            SpaceMembership.user_id == user.id,
        )
    ).scalar_one_or_none()

    assert membership is None, "an unverified address must not gain membership"
    assert invitation.status is InvitationStatus.PENDING, "the invitation must remain claimable"
    assert invitation.accepted_at is None


def test_a_missing_email_verified_claim_is_treated_as_unverified(session: Session) -> None:
    """Absence must fail closed.

    Defaulting the other way would mean any tenant misconfiguration that drops
    the claim silently reopens the hole the gate exists to close.
    """
    space_id, invitation = _make_space_with_invitation(session)

    claims = _claims()
    del claims["email_verified"]

    user = _upsert_user(session, claims)
    _claim_pending_invitations(session, user, claims)
    session.flush()

    membership = session.execute(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space_id,
            SpaceMembership.user_id == user.id,
        )
    ).scalar_one_or_none()

    assert membership is None
    assert invitation.status is InvitationStatus.PENDING


def test_a_truthy_but_non_true_email_verified_is_rejected(session: Session) -> None:
    """``"true"`` the string, or ``1``, must not pass as verification.

    The check is identity against ``True`` rather than a truthiness test, because
    a claim arriving as the string ``"false"`` is itself truthy and would
    otherwise authorise the exact attack this guards.
    """
    space_id, _ = _make_space_with_invitation(session)

    claims = _claims(email_verified="false")
    user = _upsert_user(session, claims)
    _claim_pending_invitations(session, user, claims)
    session.flush()

    membership = session.execute(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space_id,
            SpaceMembership.user_id == user.id,
        )
    ).scalar_one_or_none()

    assert membership is None


def test_a_revoked_invitation_grants_nothing_even_when_verified(session: Session) -> None:
    space_id, invitation = _make_space_with_invitation(session, status=InvitationStatus.REVOKED)

    user = _upsert_user(session, _claims())
    _claim_pending_invitations(session, user, _claims())
    session.flush()

    membership = session.execute(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space_id,
            SpaceMembership.user_id == user.id,
        )
    ).scalar_one_or_none()

    assert membership is None
    assert invitation.status is InvitationStatus.REVOKED


def test_invitation_matching_is_case_insensitive(session: Session) -> None:
    """A token bearing ``Invitee@Example.com`` must still find its invitation."""
    space_id, invitation = _make_space_with_invitation(session, email="invitee@example.com")

    claims = _claims(email="Invitee@Example.COM")
    user = _upsert_user(session, claims)
    _claim_pending_invitations(session, user, claims)
    session.flush()

    membership = session.execute(
        select(SpaceMembership).where(
            SpaceMembership.space_id == space_id,
            SpaceMembership.user_id == user.id,
        )
    ).scalar_one_or_none()

    assert membership is not None
    assert invitation.status is InvitationStatus.ACCEPTED


# --- Just-in-time provisioning. ---------------------------------------------


def test_first_request_creates_the_user(session: Session) -> None:
    user = _upsert_user(session, _claims())
    session.flush()

    assert user.id is not None
    assert user.auth0_sub == "auth0|invitee"
    assert user.email == "invitee@example.com"
    assert user.last_login_at is not None


def test_second_request_updates_rather_than_duplicating(session: Session) -> None:
    first = _upsert_user(session, _claims())
    session.flush()
    first_id, first_login = first.id, first.last_login_at

    second = _upsert_user(session, _claims(name="Renamed", email="moved@example.com"))
    session.flush()

    assert second.id == first_id, "the same sub must not create a second row"
    assert second.name == "Renamed", "Auth0 is authoritative for the profile"
    assert second.email == "moved@example.com"
    assert second.last_login_at >= first_login

    assert session.execute(select(User)).scalars().all() == [second]


def test_two_subs_may_share_an_email(session: Session) -> None:
    """A database signup and a Google login of one address are separate users.

    This is why ``users.email`` is not unique — and therefore why the
    ``email_verified`` gate above has to exist.
    """
    database_user = _upsert_user(session, _claims(sub="auth0|db"))
    google_user = _upsert_user(session, _claims(sub="google-oauth2|123"))
    session.flush()

    assert database_user.id != google_user.id
    assert database_user.email == google_user.email


# --- Header parsing. --------------------------------------------------------


def test_a_missing_authorization_header_is_rejected() -> None:
    with pytest.raises(AuthError, match="missing"):
        extract_bearer_token(None)


@pytest.mark.parametrize(
    "header",
    ["Basic abc123", "Bearer", "Bearer a b", "abc123"],
    ids=["wrong-scheme", "no-credential", "too-many-parts", "no-scheme"],
)
def test_a_malformed_authorization_header_is_rejected(header: str) -> None:
    with pytest.raises(AuthError, match="Bearer"):
        extract_bearer_token(header)


def test_the_bearer_scheme_is_matched_case_insensitively() -> None:
    """RFC 7235 defines the scheme as case-insensitive and some clients send it
    lowercase; rejecting those would be a confusing intermittent 401."""
    assert extract_bearer_token("bearer abc123") == "abc123"
    assert extract_bearer_token("Bearer abc123") == "abc123"
