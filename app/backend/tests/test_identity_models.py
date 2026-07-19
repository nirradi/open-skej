"""Constraint tests for Stream 2's identity schema, run against real Postgres.

These assert database-level guarantees — unique indexes, partial unique indexes
and foreign keys — so they need the actual engine that enforces them. SQLite
would not do: it ignores ``postgresql_where`` entirely, so the partial indexes
would degrade to plain unique ones and the most important tests here would fail
for a reason that has nothing to do with the schema being wrong.

Following ``tests/test_migrations.py``, the module skips wholesale when
``DATABASE_URL`` is unset, so Stream 1's SQLite suite keeps running standalone.
CI provides a ``postgres:16`` service and sets the variable.
"""

import os
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Base, utcnow
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

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; identity constraint tests need `docker compose up -d`",
)

# Only Stream 2's tables. Creating from metadata rather than running Alembic
# keeps these tests about the *model* definitions; test_migrations.py separately
# proves the migration matches. Passing an explicit list also stops
# `create_all` from creating Stream 1's `bookings`, which shares this metadata.
IDENTITY_TABLES = [
    User.__table__,
    Space.__table__,
    SpaceMembership.__table__,
    SpaceAccessRequest.__table__,
    SpaceInvitation.__table__,
]


@pytest.fixture(scope="module")
def engine():
    engine = create_engine(DATABASE_URL)
    Base.metadata.drop_all(engine, tables=IDENTITY_TABLES)
    Base.metadata.create_all(engine, tables=IDENTITY_TABLES)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine, tables=IDENTITY_TABLES)
        engine.dispose()


@pytest.fixture
def session(engine):
    """A session whose writes are always rolled back.

    Each test runs inside an outer transaction that is discarded afterwards, so
    tests cannot leak rows into each other — which matters here more than usual,
    since almost every test deliberately violates a uniqueness constraint and a
    stray surviving row would make a later test fail for the wrong reason.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        # Most tests here provoke an IntegrityError, which leaves the session
        # needing a rollback; closing it performs that rollback and unwinds this
        # transaction too. Rolling back an already-finished transaction is
        # harmless but warns, so only do it for the tests that stayed clean.
        if transaction.is_active:
            transaction.rollback()
        connection.close()


# --- Builders. Each flushes so the caller gets a real primary key. -----------


def _make_user(session: Session, sub: str = "auth0|alice", email: str = "alice@example.com"):
    user = User(auth0_sub=sub, email=email, name="Alice")
    session.add(user)
    session.flush()
    return user


def _make_space(session: Session, owner: User, name: str = "Court 1"):
    space = Space(public_id=generate_public_id(), name=name, created_by_user_id=owner.id)
    session.add(space)
    session.flush()
    return space


def _deny(request: SpaceAccessRequest, decider: User) -> None:
    """Move a request to `denied`, satisfying the decision-completeness CHECK."""
    request.status = AccessRequestStatus.DENIED
    request.decided_at = utcnow()
    request.decided_by_user_id = decider.id


# --- Uniqueness -------------------------------------------------------------


def test_duplicate_auth0_sub_is_rejected(session):
    """`auth0_sub` is the identity join key; two rows for one Auth0 user would
    make "which user is this token?" ambiguous."""
    _make_user(session, sub="auth0|dup", email="one@example.com")
    session.add(User(auth0_sub="auth0|dup", email="two@example.com"))

    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_email_is_allowed(session):
    """The same address under two different `sub` values is legitimate.

    Auth0 treats a database signup and a Google login as separate identities, so
    a unique constraint on email would turn an ordinary second login into a hard
    failure. Asserted explicitly because it is the kind of constraint someone
    would otherwise add "for tidiness".
    """
    _make_user(session, sub="auth0|db", email="same@example.com")
    _make_user(session, sub="google-oauth2|123", email="same@example.com")

    session.flush()


def test_duplicate_space_public_id_is_rejected(session):
    """`public_id` is the capability that grants link access; a collision would
    hand one Space's link-holders access to another."""
    owner = _make_user(session)
    space = _make_space(session, owner)
    session.add(Space(public_id=space.public_id, name="Court 2", created_by_user_id=owner.id))

    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_membership_is_rejected(session):
    """One membership per user per Space.

    Two rows at different roles would make every permission check depend on
    which one the query happened to read first.
    """
    owner = _make_user(session)
    space = _make_space(session, owner)
    session.add(SpaceMembership(space_id=space.id, user_id=owner.id, role=MembershipRole.OWNER))
    session.flush()

    session.add(SpaceMembership(space_id=space.id, user_id=owner.id, role=MembershipRole.MEMBER))
    with pytest.raises(IntegrityError):
        session.flush()


def test_same_user_may_join_two_spaces(session):
    """The membership constraint is on the *pair*, not on user alone."""
    owner = _make_user(session)
    space_a = _make_space(session, owner, name="Court A")
    space_b = _make_space(session, owner, name="Court B")

    session.add(SpaceMembership(space_id=space_a.id, user_id=owner.id))
    session.add(SpaceMembership(space_id=space_b.id, user_id=owner.id))
    session.flush()


# --- The access-request partial index ---------------------------------------
#
# Both halves below are required, and only the pair proves the index is genuinely
# partial. A plain UNIQUE (space_id, user_id) would pass the first test and fail
# the second, because it would forbid re-requesting after a denial. A missing
# index entirely would fail the first and pass the second.


def test_second_pending_access_request_is_rejected(session):
    user = _make_user(session)
    space = _make_space(session, user)
    session.add(SpaceAccessRequest(space_id=space.id, user_id=user.id, message="please"))
    session.flush()

    session.add(SpaceAccessRequest(space_id=space.id, user_id=user.id, message="please again"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_new_pending_access_request_is_allowed_after_a_denial(session):
    """A denied user may ask again, and the denial is retained as history."""
    user = _make_user(session)
    admin = _make_user(session, sub="auth0|admin", email="admin@example.com")
    space = _make_space(session, admin)

    first = SpaceAccessRequest(space_id=space.id, user_id=user.id)
    session.add(first)
    session.flush()
    _deny(first, admin)
    session.flush()

    session.add(SpaceAccessRequest(space_id=space.id, user_id=user.id, message="reformed"))
    session.flush()

    rows = session.query(SpaceAccessRequest).filter_by(space_id=space.id, user_id=user.id).all()
    assert len(rows) == 2, "the denied request must be kept as history, not overwritten"
    assert {row.status for row in rows} == {
        AccessRequestStatus.DENIED,
        AccessRequestStatus.PENDING,
    }


def test_two_denied_access_requests_may_coexist(session):
    """The partial index constrains pending rows only, with no cap on decided
    ones — otherwise a user could only ever be denied twice."""
    user = _make_user(session)
    admin = _make_user(session, sub="auth0|admin", email="admin@example.com")
    space = _make_space(session, admin)

    for _ in range(2):
        request = SpaceAccessRequest(space_id=space.id, user_id=user.id)
        session.add(request)
        session.flush()
        _deny(request, admin)
        session.flush()


def test_decided_access_request_must_record_its_decider(session):
    """The decision-completeness CHECK rejects a status flip that leaves
    `decided_at`/`decided_by_user_id` unset — a half-applied approval."""
    user = _make_user(session)
    space = _make_space(session, user)
    request = SpaceAccessRequest(space_id=space.id, user_id=user.id)
    session.add(request)
    session.flush()

    request.status = AccessRequestStatus.APPROVED
    with pytest.raises(IntegrityError):
        session.flush()


# --- The invitation partial index -------------------------------------------
# Same both-halves argument as above.


def test_second_pending_invitation_for_an_email_is_rejected(session):
    admin = _make_user(session)
    space = _make_space(session, admin)
    session.add(
        SpaceInvitation(space_id=space.id, email="bob@example.com", invited_by_user_id=admin.id)
    )
    session.flush()

    session.add(
        SpaceInvitation(
            space_id=space.id,
            email="bob@example.com",
            role=MembershipRole.ADMIN,
            invited_by_user_id=admin.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_new_pending_invitation_is_allowed_after_a_revocation(session):
    """A revoked address can be invited again; the revoked row is retained."""
    admin = _make_user(session)
    space = _make_space(session, admin)

    first = SpaceInvitation(space_id=space.id, email="bob@example.com", invited_by_user_id=admin.id)
    session.add(first)
    session.flush()
    first.status = InvitationStatus.REVOKED
    session.flush()

    session.add(
        SpaceInvitation(space_id=space.id, email="bob@example.com", invited_by_user_id=admin.id)
    )
    session.flush()

    rows = session.query(SpaceInvitation).filter_by(space_id=space.id, email="bob@example.com")
    assert {row.status for row in rows} == {InvitationStatus.REVOKED, InvitationStatus.PENDING}


def test_same_email_may_be_invited_to_two_spaces(session):
    admin = _make_user(session)
    space_a = _make_space(session, admin, name="Court A")
    space_b = _make_space(session, admin, name="Court B")

    session.add(
        SpaceInvitation(space_id=space_a.id, email="bob@example.com", invited_by_user_id=admin.id)
    )
    session.add(
        SpaceInvitation(space_id=space_b.id, email="bob@example.com", invited_by_user_id=admin.id)
    )
    session.flush()


def test_uppercase_invitation_email_is_rejected(session):
    """Task 2.3 claims invitations by looking up the JWT's email verbatim, so a
    mixed-case stored address would be an invitation that can never be claimed."""
    admin = _make_user(session)
    space = _make_space(session, admin)
    session.add(
        SpaceInvitation(space_id=space.id, email="Bob@Example.com", invited_by_user_id=admin.id)
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_accepted_invitation_must_record_accepted_at(session):
    admin = _make_user(session)
    space = _make_space(session, admin)
    invitation = SpaceInvitation(
        space_id=space.id, email="bob@example.com", invited_by_user_id=admin.id
    )
    session.add(invitation)
    session.flush()

    invitation.status = InvitationStatus.ACCEPTED
    with pytest.raises(IntegrityError):
        session.flush()


# --- public_id entropy ------------------------------------------------------


def test_public_ids_are_high_entropy_and_collision_free():
    """The link *is* the capability, so `public_id` must be neither sequential
    nor short. A `random`-based or counter-based generator would show up here as
    either a collision or a short value.
    """
    generated = [generate_public_id() for _ in range(10_000)]

    assert len(set(generated)) == len(generated), "public_id generator produced a collision"
    assert min(len(value) for value in generated) >= 20


def test_short_public_id_is_rejected_by_the_database(session):
    """Even if application code ever supplies its own `public_id`, the CHECK
    stops a guessable one from reaching the table."""
    owner = _make_user(session)
    session.add(Space(public_id="short", name="Court 1", created_by_user_id=owner.id))

    with pytest.raises(IntegrityError):
        session.flush()


# --- Foreign keys -----------------------------------------------------------


@pytest.mark.parametrize(
    "make_row",
    [
        pytest.param(
            lambda space, user: Space(
                public_id=generate_public_id(), name="Orphan", created_by_user_id=10_000_001
            ),
            id="space.created_by_user_id",
        ),
        pytest.param(
            lambda space, user: SpaceMembership(space_id=10_000_001, user_id=user.id),
            id="membership.space_id",
        ),
        pytest.param(
            lambda space, user: SpaceMembership(space_id=space.id, user_id=10_000_001),
            id="membership.user_id",
        ),
        pytest.param(
            lambda space, user: SpaceAccessRequest(space_id=10_000_001, user_id=user.id),
            id="access_request.space_id",
        ),
        pytest.param(
            lambda space, user: SpaceInvitation(
                space_id=space.id, email="bob@example.com", invited_by_user_id=10_000_001
            ),
            id="invitation.invited_by_user_id",
        ),
    ],
)
def test_foreign_key_violations_are_rejected(session, make_row):
    """Parametrised per foreign key rather than asserted once: a single case
    would pass even if only that one key were declared."""
    user = _make_user(session)
    space = _make_space(session, user)

    session.add(make_row(space, user))
    with pytest.raises(IntegrityError):
        session.flush()


def test_access_request_decided_by_may_reference_a_different_user(session):
    """`decided_by_user_id` points at the deciding admin, not the requester."""
    requester = _make_user(session)
    admin = _make_user(session, sub="auth0|admin", email="admin@example.com")
    space = _make_space(session, admin)

    request = SpaceAccessRequest(space_id=space.id, user_id=requester.id)
    session.add(request)
    session.flush()

    request.status = AccessRequestStatus.APPROVED
    request.decided_at = utcnow() + timedelta(seconds=1)
    request.decided_by_user_id = admin.id
    session.flush()

    assert request.decided_by_user_id != request.user_id
