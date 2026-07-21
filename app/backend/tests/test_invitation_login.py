"""The invitation pre-approval path, end to end: invite, then log in.

This module exists because the two halves of an invitation live in different
places and only mean something together. Task 2.7 writes the row through
``POST /spaces/{public_id}/invitations``; task 2.3's
``_claim_pending_invitations`` turns it into a membership at login. Either half
can be correct while the pair is broken — an invitation stored with the wrong
casing, or at a role the claim ignores, passes every test written against one
side alone.

So unlike ``tests/test_spaces_api.py``, this module does **not** override
``get_current_user``. It mints a real RS256 token against an in-process keypair
and overrides only the JWKS resolver, so the request travels the production path:
verify the token, upsert the user, claim the invitation, commit. That is the only
arrangement in which "the invited user logs in" is a statement about the system
rather than about a fixture.

**The assertion that carries the plan's requirement is the one about access
requests.** A membership appearing at login is equally consistent with an
implementation that quietly files an access request and auto-approves it — which
is a different product, audits differently, and would put a decided row in every
admin's queue. Zero access-request rows is what distinguishes genuine
pre-approval from an auto-approval, so it is asserted explicitly rather than
implied.

Postgres-only, like its siblings: the partial unique indexes are Postgres
features, so the module skips when ``DATABASE_URL`` is unset.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.auth.jwt import TokenVerifier, get_token_verifier
from app.db.models import Base
from app.db.session import get_session
from app.identity import service
from app.identity.models import (
    InvitationStatus,
    MembershipRole,
    Space,
    SpaceAccessRequest,
    SpaceInvitation,
    SpaceMembership,
    User,
)
from app.main import app

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; the invitation login flow needs `docker compose up -d`",
)

DOMAIN = "test-tenant.us.auth0.com"
ISSUER = f"https://{DOMAIN}/"
AUDIENCE = "https://api.open-skej.dev"

INVITEE_EMAIL = "invitee@example.com"
INVITEE_SUB = "auth0|invitee"


# --- The stub JWKS, mirroring tests/test_auth_jwt.py. -----------------------


class _StubKey:
    """Mimics ``PyJWK``, which exposes the key material as ``.key``."""

    def __init__(self, key: object) -> None:
        self.key = key


class _StubResolver:
    """Stands in for ``PyJWKClient``, always returning one known public key."""

    def __init__(self, public_key: object) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> _StubKey:
        return _StubKey(self._public_key)


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(
    private_key: rsa.RSAPrivateKey,
    *,
    sub: str = INVITEE_SUB,
    email: str = INVITEE_EMAIL,
    email_verified: bool = True,
    name: str = "Invitee",
) -> str:
    """A token the real verifier will accept, for whoever is logging in."""
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "sub": sub,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "email": email,
        "email_verified": email_verified,
        "name": name,
    }
    return jwt.encode(claims, private_key, algorithm="RS256")


# --- Fixtures. --------------------------------------------------------------


@pytest.fixture
def engine():
    engine = create_engine(DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def client(session: Session, private_key: rsa.RSAPrivateKey) -> Iterator[TestClient]:
    """A client that authenticates for real.

    ``get_current_user`` is deliberately *not* overridden — overriding it is what
    would skip the invitation claim this module exists to exercise. Only the JWKS
    resolver is stubbed, so no network and no Auth0 credentials are needed.
    """
    verifier = TokenVerifier(
        domain=DOMAIN,
        audience=AUDIENCE,
        jwks_client=_StubResolver(private_key.public_key()),
    )

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def owner(session: Session) -> User:
    user = User(auth0_sub="auth0|owner", email="owner@example.com", name="Owner")
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def space(session: Session, owner: User) -> Space:
    return service.create_space(session, owner, name="Centre Court", description=None)


def _invite(
    session: Session,
    space: Space,
    owner: User,
    *,
    email: str = INVITEE_EMAIL,
    role: MembershipRole = MembershipRole.ADMIN,
) -> SpaceInvitation:
    """Create an invitation through the service the API calls.

    Through ``service.create_invitation`` rather than by constructing the row, so
    that what the login side consumes is genuinely what task 2.7's write path
    produces — including its lowercasing. A hand-built row would let the two
    halves disagree in exactly the way this module exists to catch.
    """
    return service.create_invitation(
        session,
        space,
        owner,
        email=email.strip().lower(),
        role=role,
        inviter_role=MembershipRole.OWNER,
    )


def _login(client: TestClient, private_key: rsa.RSAPrivateKey, **token_kwargs: Any):
    """One authenticated request — the moment the invitation is claimed.

    ``GET /me`` because it is the cheapest authenticated route; the claiming
    happens in the dependency, so any route would do.
    """
    return client.get(
        "/me", headers={"Authorization": f"Bearer {_token(private_key, **token_kwargs)}"}
    )


def _membership(session: Session, space: Space, email: str) -> SpaceMembership | None:
    return session.execute(
        select(SpaceMembership)
        .join(User, User.id == SpaceMembership.user_id)
        .where(SpaceMembership.space_id == space.id, func.lower(User.email) == email.lower())
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _invitation_row(session: Session, invitation_id: int) -> SpaceInvitation:
    return session.execute(
        select(SpaceInvitation)
        .where(SpaceInvitation.id == invitation_id)
        .execution_options(populate_existing=True)
    ).scalar_one()


# --- The headline: pre-approval, end to end. --------------------------------


def test_an_invited_user_lands_inside_the_space_on_first_login(
    client: TestClient, session: Session, private_key: rsa.RSAPrivateKey, owner: User, space: Space
) -> None:
    """Invite an address, log in as it, be a member — at the invited role.

    The role assertion is not incidental. An implementation that created every
    invited user as a plain ``member`` would satisfy "is in the Space" while
    quietly discarding the authority the inviter chose to delegate, and the
    admin would have to notice and fix it by hand.
    """
    invitation = _invite(session, space, owner, role=MembershipRole.ADMIN)

    response = _login(client, private_key)
    assert response.status_code == 200, response.text

    membership = _membership(session, space, INVITEE_EMAIL)
    assert membership is not None, "the invitation did not admit the invitee"
    assert membership.role is MembershipRole.ADMIN, "the invited role must be honoured"

    assert _invitation_row(session, invitation.id).status is InvitationStatus.ACCEPTED
    assert _invitation_row(session, invitation.id).accepted_at is not None

    # And the access is real, not merely recorded: the Space's member-only
    # detail route now answers for a caller who was a stranger a moment ago.
    detail = client.get(
        f"/spaces/{space.public_id}",
        headers={"Authorization": f"Bearer {_token(private_key)}"},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["my_role"] == "admin"


def test_pre_approval_files_no_access_request(
    client: TestClient, session: Session, private_key: rsa.RSAPrivateKey, owner: User, space: Space
) -> None:
    """**The assertion that distinguishes pre-approval from auto-approval.**

    A membership appearing at login is equally consistent with an implementation
    that files an access request on the invitee's behalf and immediately approves
    it. The user would end up in the same place, so the happy path above cannot
    tell the two apart — but the admin's queue would carry a decided row nobody
    ever decided, the audit trail would name a decider who never looked, and
    ``/preview`` would report a request that was never made.

    Counted across the whole table rather than filtered to this Space: a stray
    row written against the wrong ``space_id`` is exactly the kind of bug a
    scoped count would hide.
    """
    _invite(session, space, owner)

    assert _login(client, private_key).status_code == 200

    assert (
        _membership(session, space, INVITEE_EMAIL) is not None
    ), "no membership was created, so the absence of access requests proves nothing"

    requests = session.execute(select(SpaceAccessRequest)).scalars().all()
    assert requests == [], (
        "an invitation must pre-approve, not file and auto-approve a request;"
        f" found {len(requests)} access request row(s)"
    )

    # The invitee's own view agrees: they are a member, and were never a pending
    # or denied requester on the way there.
    preview = client.get(
        f"/spaces/{space.public_id}/preview",
        headers={"Authorization": f"Bearer {_token(private_key)}"},
    )
    assert preview.json()["status"] == "member"


def test_the_invited_address_matches_case_insensitively_end_to_end(
    client: TestClient, session: Session, private_key: rsa.RSAPrivateKey, owner: User, space: Space
) -> None:
    """Invited as ``X@Y.com``, logging in as ``x@y.com``, and still admitted.

    The two sides normalise independently — ``InvitationCreate`` lowercases on
    the way in, ``_claim_pending_invitations`` lowercases the token's address on
    the way out — so this is the test that proves they agree. If either side
    stopped normalising, the invitation would simply never match and the invitee
    would be told to request access like a stranger: a silent failure, with no
    error anywhere to notice.
    """
    invitation = _invite(session, space, owner, email="MiXeD@ExAmPlE.CoM")
    assert invitation.email == "mixed@example.com"

    assert (
        _login(client, private_key, sub="auth0|mixed", email="mixed@EXAMPLE.com").status_code == 200
    )

    assert _membership(session, space, "mixed@example.com") is not None
    assert _invitation_row(session, invitation.id).status is InvitationStatus.ACCEPTED


# --- What must *not* grant access. ------------------------------------------


def test_a_revoked_invitation_grants_nothing_at_login(
    client: TestClient, session: Session, private_key: rsa.RSAPrivateKey, owner: User, space: Space
) -> None:
    """Revocation is only meaningful if it holds at the moment it is tested.

    Asserted through the revoke *endpoint* rather than by writing ``REVOKED``
    directly: this is the pair to task 2.7's ``DELETE`` route, and a revocation
    that set some other field, or a claim that ignored ``status``, would still
    pass a test that hand-wrote the row.
    """
    invitation = _invite(session, space, owner)

    revoked = client.delete(
        f"/spaces/{space.public_id}/invitations/{invitation.id}",
        headers={
            "Authorization": f"Bearer {_token(private_key, sub='auth0|owner', email=owner.email)}"
        },
    )
    assert revoked.status_code == 200, revoked.text

    assert _login(client, private_key).status_code == 200

    assert (
        _membership(session, space, INVITEE_EMAIL) is None
    ), "a revoked invitation admitted its invitee anyway"
    assert _invitation_row(session, invitation.id).status is InvitationStatus.REVOKED
    assert session.execute(select(SpaceAccessRequest)).scalars().all() == []


def test_an_unverified_address_claims_nothing_through_the_api(
    client: TestClient, session: Session, private_key: rsa.RSAPrivateKey, owner: User, space: Space
) -> None:
    """Task 2.3's security gate, re-asserted over HTTP now that 2.7 can write the row.

    ``tests/test_auth_dependencies.py`` proves this against the function.
    Repeating it through the full request path is not duplication: it is what
    rules out the gate being bypassed by the wiring — a route that claimed
    invitations itself, or a dependency ordering that ran the claim before
    verification.
    """
    invitation = _invite(session, space, owner)

    assert _login(client, private_key, email_verified=False).status_code == 200

    assert _membership(session, space, INVITEE_EMAIL) is None
    assert (
        _invitation_row(session, invitation.id).status is InvitationStatus.PENDING
    ), "the invitation must stay claimable by whoever actually owns the address"
