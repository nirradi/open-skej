"""Tests for the deterministic sandbox seed (task 4.8), run against real Postgres.

Postgres-only, following ``tests/test_spaces_api.py``: partial unique indexes
and ``with_for_update`` locking back several of ``app.identity.service``'s
invariants, and SQLite honours neither. The module skips wholesale when
``DATABASE_URL`` is unset.
"""

import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.constants import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID
from app.db.models import Base, Booking
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
from app.sandbox_seed import (
    ADMIN_AUTH0_SUB,
    ADMIN_EMAIL,
    MEMBER_AUTH0_SUB,
    MEMBER_EMAIL,
    OWNER_AUTH0_SUB,
    OWNER_EMAIL,
    PENDING_INVITEE_EMAIL,
    RESOURCE_A1_CLOSES_AT,
    RESOURCE_A1_OPENS_AT,
    RESOURCE_A1_SLOT_MINUTES,
    RESOURCE_A2_CLOSES_AT,
    RESOURCE_A2_OPENS_AT,
    RESOURCE_A2_SLOT_MINUTES,
    SPACE_A_NAME,
    SPACE_A_TIMEZONE,
    SPACE_B_NAME,
    SPACE_B_TIMEZONE,
    STRANGER_AUTH0_SUB,
    STRANGER_EMAIL,
    run,
)

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; the sandbox seed needs `docker compose up -d`",
)


@pytest.fixture
def session(pg_engine):
    """A session over a freshly built schema, dropped again afterwards.

    Built from ``Base.metadata`` rather than Alembic, matching ``conftest.
    driver``: the seed itself is what is under test here, not the migration.
    """
    Base.metadata.drop_all(pg_engine)
    Base.metadata.create_all(pg_engine)
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(pg_engine)


def _count(session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_seed_produces_every_interesting_state(session):
    run(session)

    # The four deterministic identities exist, by the subs the seed documents.
    owner = session.execute(select(User).where(User.auth0_sub == OWNER_AUTH0_SUB)).scalar_one()
    admin = session.execute(select(User).where(User.auth0_sub == ADMIN_AUTH0_SUB)).scalar_one()
    member = session.execute(select(User).where(User.auth0_sub == MEMBER_AUTH0_SUB)).scalar_one()
    stranger = session.execute(
        select(User).where(User.auth0_sub == STRANGER_AUTH0_SUB)
    ).scalar_one()
    assert owner.email == OWNER_EMAIL
    assert admin.email == ADMIN_EMAIL
    assert member.email == MEMBER_EMAIL
    assert stranger.email == STRANGER_EMAIL

    # Space A: non-UTC, and carries all three roles.
    space_a = session.execute(select(Space).where(Space.name == SPACE_A_NAME)).scalar_one()
    assert space_a.timezone == SPACE_A_TIMEZONE
    assert space_a.archived_at is None

    roles_in_a = dict(
        session.execute(
            select(SpaceMembership.user_id, SpaceMembership.role).where(
                SpaceMembership.space_id == space_a.id
            )
        ).all()
    )
    assert roles_in_a[owner.id] == MembershipRole.OWNER
    assert roles_in_a[admin.id] == MembershipRole.ADMIN
    assert roles_in_a[member.id] == MembershipRole.MEMBER
    assert stranger.id not in roles_in_a

    # Space A's two Resources are configured differently from one another.
    resources_a = (
        session.execute(
            select(Resource).where(Resource.space_id == space_a.id).order_by(Resource.id)
        )
        .scalars()
        .all()
    )
    assert len(resources_a) == 2
    configs = {(r.opens_at, r.closes_at, r.slot_minutes) for r in resources_a}
    assert configs == {
        (RESOURCE_A1_OPENS_AT, RESOURCE_A1_CLOSES_AT, RESOURCE_A1_SLOT_MINUTES),
        (RESOURCE_A2_OPENS_AT, RESOURCE_A2_CLOSES_AT, RESOURCE_A2_SLOT_MINUTES),
    }

    # Space B: a different tenant, in a different zone, that neither the
    # member nor the stranger belongs to — the cross-tenant isolation fixture.
    space_b = session.execute(select(Space).where(Space.name == SPACE_B_NAME)).scalar_one()
    assert space_b.timezone == SPACE_B_TIMEZONE
    member_ids_in_b = set(
        session.execute(
            select(SpaceMembership.user_id).where(SpaceMembership.space_id == space_b.id)
        )
        .scalars()
        .all()
    )
    assert member.id not in member_ids_in_b
    assert stranger.id not in member_ids_in_b
    assert owner.id in member_ids_in_b

    # A pending access request, filed by the stranger against Space A.
    access_request = session.execute(
        select(SpaceAccessRequest).where(
            SpaceAccessRequest.space_id == space_a.id,
            SpaceAccessRequest.user_id == stranger.id,
        )
    ).scalar_one()
    assert access_request.status == AccessRequestStatus.PENDING

    # A pending invitation, addressed to someone with no `users` row.
    invitation = session.execute(
        select(SpaceInvitation).where(
            SpaceInvitation.space_id == space_a.id,
            SpaceInvitation.email == PENDING_INVITEE_EMAIL,
        )
    ).scalar_one()
    assert invitation.status == InvitationStatus.PENDING
    assert (
        session.execute(
            select(User).where(User.email == PENDING_INVITEE_EMAIL)
        ).scalar_one_or_none()
        is None
    )

    # An archived Space exists, distinct from Space A and Space B.
    archived_spaces = (
        session.execute(select(Space).where(Space.archived_at.is_not(None))).scalars().all()
    )
    assert len(archived_spaces) == 1
    assert archived_spaces[0].id not in (space_a.id, space_b.id)

    # The default booking target the unscoped `POST /bookings` still needs.
    assert session.get(User, DEFAULT_USER_ID) is not None
    assert session.get(Resource, DEFAULT_RESOURCE_ID) is not None


def test_seed_is_idempotent_reset_not_accumulate(session):
    """Running the seed twice yields identical row counts — a reset, not a pile-up."""
    run(session)
    counts_first = {
        model: _count(session, model)
        for model in (
            User,
            Space,
            Resource,
            SpaceMembership,
            SpaceAccessRequest,
            SpaceInvitation,
            Booking,
        )
    }

    run(session)
    counts_second = {
        model: _count(session, model)
        for model in (
            User,
            Space,
            Resource,
            SpaceMembership,
            SpaceAccessRequest,
            SpaceInvitation,
            Booking,
        )
    }

    assert counts_first == counts_second
    # Not a trivial all-zero comparison: real rows exist both times.
    assert counts_first[User] > 0
    assert counts_first[Space] > 0

    # The default booking target specifically survives the second run, not
    # just some row with the same count.
    assert session.get(User, DEFAULT_USER_ID) is not None
    assert session.get(Resource, DEFAULT_RESOURCE_ID) is not None
