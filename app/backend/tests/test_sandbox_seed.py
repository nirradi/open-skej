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
    ShapeStatus,
    Space,
    SpaceAccessRequest,
    SpaceCalendarShape,
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
    SPACE_A_SHAPE,
    SPACE_A_TIMEZONE,
    SPACE_B_BLACKOUT_REASON,
    SPACE_B_MAX_BOOKINGS_PER_WEEK,
    SPACE_B_MAX_DURATION_MINUTES,
    SPACE_B_NAME,
    SPACE_B_SHAPE,
    SPACE_B_STEP_MINUTES,
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


def _live_shape_document(session, space) -> dict:
    """This Space's one live calendar-shape document, as stored.

    Read as the raw document rather than through ``service.live_shape``'s
    validated ``Shape``: what this module asserts is that the seed planted the
    document it declares, and comparing dataclasses would only prove the two
    validate to the same thing, which is a weaker claim than the seed's own
    constants being what landed.
    """
    row = session.execute(
        select(SpaceCalendarShape).where(
            SpaceCalendarShape.space_id == space.id,
            SpaceCalendarShape.status == ShapeStatus.LIVE,
        )
    ).scalar_one()
    return row.document


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

    # Space A: non-UTC, and carries all three roles. No frequency cap — its
    # canon is kept to exactly what the E2E suite exercises (see the module
    # docstring), and its opening hours are its shape rather than a rule at
    # all (task 10.5).
    space_a = session.execute(select(Space).where(Space.name == SPACE_A_NAME)).scalar_one()
    assert space_a.timezone == SPACE_A_TIMEZONE
    # Exactly three rules, and the set is asserted rather than each member: a
    # fourth type appearing here is a rule the E2E suite never configured and
    # could be denied by for a reason it does not assert on.
    # `max_consecutive_duration` is task 8.9's own addition — the
    # cross-Resource guard needs it configured, and it is deliberately not a
    # frequency cap in the sense the comment above means (see
    # `sandbox_seed.py`'s own comment on the constant).
    rules_a = _rules(session, space_a)
    assert set(rules_a) == {
        "max_duration",
        "max_consecutive_duration",
        "booking_horizon",
    }
    assert rules_a["max_duration"] == {"max_duration_minutes": SPACE_A_MAX_DURATION_MINUTES}
    assert rules_a["max_consecutive_duration"] == {
        "max_consecutive_minutes": SPACE_A_MAX_CONSECUTIVE_MINUTES
    }
    assert rules_a["booking_horizon"] == {"days": SPACE_A_BOOKING_HORIZON_DAYS}
    assert space_a.archived_at is None

    # Its live shape is the seed's own document, not the `DEFAULT_SHAPE`
    # `create_space` writes — the property `03-sad-path.spec.ts` depends on.
    # That spec drags past two hours specifically to be refused by
    # `max_duration` with that rule's own copy, which only happens if the
    # shape offers a booking that long: `DEFAULT_SHAPE` offers 60 minutes and
    # the availability gate, running ahead of the engine, would refuse first
    # with the wrong message entirely.
    assert _live_shape_document(session, space_a) == SPACE_A_SHAPE
    assert 150 in SPACE_A_SHAPE["operating_blocks"][0]["allowed_durations_mins"]

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
    # Space B is where the capabilities Space A deliberately skips are
    # observable: a weekly cap counted across both its Resources, and — in its
    # shape rather than its rules — real operating hours to resolve per date
    # in a zone that is not Space A's, plus the seed's one blackout.
    assert set(rules_b) == {
        "max_duration",
        "max_bookings_per_week",
    }
    assert rules_b["max_duration"] == {"max_duration_minutes": SPACE_B_MAX_DURATION_MINUTES}
    assert rules_b["max_bookings_per_week"] == {"max_bookings": SPACE_B_MAX_BOOKINGS_PER_WEEK}
    assert rules_b != rules_a

    # Space B's shape is the interesting one: a real operating window rather
    # than the 24-hour default, and a blackout with member-facing copy, so the
    # seeded product demonstrates the feature it now has (task 10.5). Space A
    # and Space B differ in shape as well as in rules, which is what makes the
    # two Spaces genuinely distinct fixtures.
    shape_b = _live_shape_document(session, space_b)
    assert shape_b == SPACE_B_SHAPE
    assert shape_b != _live_shape_document(session, space_a)
    block_b = shape_b["operating_blocks"][0]
    assert block_b["start_time"] != "00:00" or block_b["end_time"] != "24:00"
    assert min(block_b["allowed_durations_mins"]) == SPACE_B_STEP_MINUTES
    assert [window["reason"] for window in shape_b["blackout_windows"]] == [SPACE_B_BLACKOUT_REASON]

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
