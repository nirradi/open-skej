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
from app.identity import service
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
    SPACE_A_BOOKING_HORIZON_DAYS,
    SPACE_A_MAX_CONSECUTIVE_MINUTES,
    SPACE_A_MAX_DURATION_MINUTES,
    SPACE_A_NAME,
    SPACE_A_SESSION_MINUTES,
    SPACE_A_TIMEZONE,
    SPACE_B_CLOSES_AT_MINUTES,
    SPACE_B_MAX_BOOKINGS_PER_WEEK,
    SPACE_B_MAX_DURATION_MINUTES,
    SPACE_B_NAME,
    SPACE_B_OPENS_AT_MINUTES,
    SPACE_B_SESSION_MINUTES,
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


def _rules(session, space) -> dict[str, dict]:
    """This Space's unscoped rule instances, keyed by type.

    A Space's configuration is ``space_rules`` rows, so this is what the seed
    plants and what ``app.rules_stub`` assembles a canon from. A type with no
    row is not enforced at all — which is an assertion this module makes about
    Space A, not an absence it tolerates.
    """
    return {
        rule.rule_type: rule.params
        for rule in service.list_space_rules(session, space)
        if rule.applies_to is None
    }


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

    # Space A: non-UTC, and carries all three roles. No availability window
    # and no frequency cap — its canon is kept to exactly what the E2E suite
    # exercises (see the module docstring).
    space_a = session.execute(select(Space).where(Space.name == SPACE_A_NAME)).scalar_one()
    assert space_a.timezone == SPACE_A_TIMEZONE
    # Exactly four rules, and the set is asserted rather than each member:
    # `availability_hours` being absent is the fixture property `03-sad-path.
    # spec.ts` depends on, and `create_space` seeds one by default, so a seed
    # that forgot to delete it must fail here. `max_consecutive_duration` is
    # task 8.9's own addition — the cross-Resource guard needs it configured,
    # and it is deliberately not a frequency cap in the sense the comment
    # above means (see `sandbox_seed.py`'s own comment on the constant).
    rules_a = _rules(session, space_a)
    assert set(rules_a) == {
        "session_length",
        "max_duration",
        "max_consecutive_duration",
        "booking_horizon",
    }
    assert rules_a["session_length"] == {"session_minutes": SPACE_A_SESSION_MINUTES}
    assert rules_a["max_duration"] == {"max_duration_minutes": SPACE_A_MAX_DURATION_MINUTES}
    assert rules_a["max_consecutive_duration"] == {
        "max_consecutive_minutes": SPACE_A_MAX_CONSECUTIVE_MINUTES
    }
    assert rules_a["booking_horizon"] == {"days": SPACE_A_BOOKING_HORIZON_DAYS}
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

    # Space A's two Resources are identical courts sharing the Space's one
    # schedule — a Resource carries no configuration of its own.
    resources_a = (
        session.execute(
            select(Resource).where(Resource.space_id == space_a.id).order_by(Resource.id)
        )
        .scalars()
        .all()
    )
    assert len(resources_a) == 2
    assert {r.name for r in resources_a} == {"Court 1", "Court 2"}

    # Space B: a different tenant, in a different zone, that neither the
    # member nor the stranger belongs to — the cross-tenant isolation fixture.
    space_b = session.execute(select(Space).where(Space.name == SPACE_B_NAME)).scalar_one()
    assert space_b.timezone == SPACE_B_TIMEZONE
    assert space_b.timezone != space_a.timezone
    rules_b = _rules(session, space_b)
    # Configured differently from Space A — the two Spaces, not their
    # Resources, are what differ now that configuration lives on the Space.
    # Space B is where the per-Space rule capabilities Space A deliberately
    # skips are observable: real availability hours to resolve per date, and a
    # weekly cap counted across both its Resources.
    assert set(rules_b) == {
        "availability_hours",
        "session_length",
        "max_duration",
        "max_bookings_per_week",
    }
    assert rules_b["availability_hours"] == {
        "opens_at_minutes": SPACE_B_OPENS_AT_MINUTES,
        "closes_at_minutes": SPACE_B_CLOSES_AT_MINUTES,
    }
    assert rules_b["session_length"] == {"session_minutes": SPACE_B_SESSION_MINUTES}
    assert rules_b["max_duration"] == {"max_duration_minutes": SPACE_B_MAX_DURATION_MINUTES}
    assert rules_b["max_bookings_per_week"] == {"max_bookings": SPACE_B_MAX_BOOKINGS_PER_WEEK}
    assert rules_b != rules_a

    # Two Resources, like Space A — the weekly cap is Space-wide, so
    # demonstrating it needs a booking to land on a different Resource than
    # the first two (task 5.1).
    resources_b = (
        session.execute(
            select(Resource).where(Resource.space_id == space_b.id).order_by(Resource.id)
        )
        .scalars()
        .all()
    )
    assert len(resources_b) == 2
    assert {r.name for r in resources_b} == {"Main", "Court 2"}

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
