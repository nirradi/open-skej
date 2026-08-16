"""Tests for ``GET /spaces/{public_id}/resources/{resource_id}/bookable`` — the live endpoint,
focused on the properties that only show up with a real Space, a real database, and a real cache:
that two different members see the identical projection, that writing a rule invalidates the
cached answer, and that a weekday-scoped ``session_length`` resolves the right slot size on each
day of a real window. The rule-level properties (a history-reading type excluded and reported, a
generated-style rule shading the grid, a duration cap limiting ``max_slots`` rather than shading)
are ``test_projection.py``'s job, in plain Python with no database — this module is for what only
the live endpoint and ``app.projection_cache``'s process-wide singleton can show.

Postgres-only, following ``test_resource_bookings_api.py``'s own fixtures almost exactly — a Space
(with its auto-created Resource), an owner and a plain member, and a driver sharing the same engine
as the identity session.
"""

import os
from datetime import date, timedelta
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth.dependencies import get_current_user
from app.db.models import Base
from app.db.postgres import PostgresBookingDriver
from app.db.session import get_session
from app.dependencies import get_driver
from app.identity import service
from app.identity.models import MembershipRole, Resource, Space, SpaceMembership, User
from app.identity.schemas import SpaceRuleUpdate
from app.main import app
from app.projection_cache import projection_cache

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; the bookable route needs `docker compose up -d`",
)

# A Monday, chosen the same way `projection_bench.py`'s own `WINDOW_START` is: far enough in the
# future that `not_in_the_past` never denies anything here (this module does not care whether a
# slot is allowed, only what shape the response has), and a real Monday so a weekday/weekend
# `applies_to` split is unambiguous.
MONDAY = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)


def _bookable_url(space: Space, resource: Resource, from_date: date, to_date: date) -> str:
    return (
        f"/spaces/{space.public_id}/resources/{resource.id}/bookable"
        f"?from={from_date.isoformat()}&to={to_date.isoformat()}"
    )


# --- Fixtures. ------------------------------------------------------------------------------


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
def driver(engine) -> PostgresBookingDriver:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return PostgresBookingDriver(factory)


@pytest.fixture(autouse=True)
def _clear_projection_cache() -> Iterator[None]:
    """``app.projection_cache.projection_cache`` is a process-wide singleton (module docstring,
    "in-process only"), so without this, a Space id reused across tests — the ``engine`` fixture
    recreates the schema every test, and Postgres identity sequences restart with it — could serve
    a *different* test's Space's cached projection under the identical ``(space_id, date,
    rules_version, catalog_generation)`` key. Cleared both before and after so a failure mid-test
    cannot leave a poisoned entry for whichever test runs next.
    """
    projection_cache.clear()
    yield
    projection_cache.clear()


class Api:
    def __init__(self, client: TestClient, caller: dict[str, User]) -> None:
        self._client = client
        self._caller = caller

    def as_user(self, user: User) -> TestClient:
        self._caller["user"] = user
        return self._client


@pytest.fixture
def api(session: Session, driver: PostgresBookingDriver) -> Iterator[Api]:
    caller: dict[str, User] = {}

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: caller["user"]
    app.dependency_overrides[get_driver] = lambda: driver
    try:
        yield Api(TestClient(app), caller)
    finally:
        app.dependency_overrides.clear()


def _make_user(session: Session, sub: str, email: str) -> User:
    user = User(auth0_sub=sub, email=email, name=email.split("@")[0].title())
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def owner(session: Session) -> User:
    return _make_user(session, "auth0|owner", "owner@example.com")


@pytest.fixture
def member(session: Session) -> User:
    return _make_user(session, "auth0|member", "member@example.com")


@pytest.fixture
def space(session: Session, owner: User) -> Space:
    """A Space with the defaults ``create_space`` seeds: UTC, hours 09:00-17:00, a 60-minute
    unscoped ``session_length`` row — nothing that reads history, so every rule this Space starts
    with is already inside the projection (task 1)."""
    return service.create_space(session, owner, name="Court Club", description="A club")


@pytest.fixture
def resource(session: Session, space: Space) -> Resource:
    return session.execute(select(Resource).where(Resource.space_id == space.id)).scalar_one()


@pytest.fixture(autouse=True)
def _add_member(session: Session, space: Space, member: User) -> None:
    session.add(SpaceMembership(space_id=space.id, user_id=member.id, role=MembershipRole.MEMBER))
    session.commit()


# --- Two different members see the identical projection. ------------------------------------


def test_two_different_members_get_the_identical_projection(
    api: Api, owner: User, member: User, space: Space, resource: Resource
) -> None:
    """The whole point of task 1 and task 2: the projection reads no member's history and no
    Resource's own bookings, so it cannot legitimately differ by who is asking."""
    url = _bookable_url(space, resource, MONDAY, MONDAY + timedelta(days=1))

    as_owner = api.as_user(owner).get(url)
    as_member = api.as_user(member).get(url)

    assert as_owner.status_code == 200, as_owner.text
    assert as_member.status_code == 200, as_member.text
    assert as_owner.json() == as_member.json()


# --- Writing a rule invalidates the cache. ---------------------------------------------------


def test_writing_a_rule_invalidates_the_cached_projection(
    api: Api, session: Session, owner: User, space: Space, resource: Resource
) -> None:
    url = _bookable_url(space, resource, MONDAY, MONDAY + timedelta(days=1))

    before = api.as_user(owner).get(url)
    assert before.status_code == 200, before.text
    before_starts = before.json()["days"][0]["starts"]
    # The grid renders the whole local day at the 60-minute grid the seeded session_length row
    # resolves (`app.routers.bookable._grid_for_date` mirrors the frontend: it always spans
    # midnight-to-midnight, never just the open hours), so index 9 is the slot starting at 09:00 —
    # the seeded 09:00-17:00 window allows a full 8-hour run from there. Asserted here so the
    # tightened window below is a real, observable change and not two already-identical responses
    # agreeing by coincidence.
    NINE_AM_SLOT = 9
    assert before_starts[NINE_AM_SLOT] == [1, 8]

    availability = next(
        rule
        for rule in service.list_space_rules(session, space)
        if rule.rule_type == "availability_hours"
    )
    service.update_space_rule(
        session,
        space,
        rule_id=availability.id,
        payload=SpaceRuleUpdate(params={"opens_at_minutes": 540, "closes_at_minutes": 600}),
    )

    after = api.as_user(owner).get(url)
    assert after.status_code == 200, after.text
    after_starts = after.json()["days"][0]["starts"]

    assert after_starts != before_starts
    # Closing at 10:00 instead of 17:00 leaves exactly one hour, one slot, from the same 09:00
    # start — the cache must have actually recomputed against the new rule, not merely returned
    # something different by accident.
    assert after_starts[NINE_AM_SLOT] == [1, 1]


# --- A weekday-scoped session_length resolves the right slot size per day. -------------------


def test_weekday_scoped_session_length_reports_the_right_slot_size_per_day(
    api: Api, session: Session, owner: User, space: Space, resource: Resource
) -> None:
    """``create_space`` seeds one unscoped 60-minute ``session_length`` row; this test replaces it
    with two ``applies_to``-scoped rows — a finer weekend grid, mirroring a real club that runs a
    coarser grid on weekdays and a finer one on weekends — and checks that each day in a
    Monday-to-Sunday window reports its *own* resolved ``slotMinutes``, not the first day's for
    every day (the bug ``POC.md`` calls out for the pre-task-3 shape of this endpoint)."""
    default_session_length = next(
        rule
        for rule in service.list_space_rules(session, space)
        if rule.rule_type == "session_length"
    )
    service.delete_space_rule(session, space, rule_id=default_session_length.id)
    service.create_space_rule(
        session,
        space,
        rule_type="session_length",
        params={"session_minutes": 60},
        applies_to={"weekdays": [0, 1, 2, 3, 4]},
        enabled=True,
    )
    service.create_space_rule(
        session,
        space,
        rule_type="session_length",
        params={"session_minutes": 15},
        applies_to={"weekdays": [5, 6]},
        enabled=True,
    )

    url = _bookable_url(space, resource, MONDAY, MONDAY + timedelta(days=7))
    response = api.as_user(owner).get(url)
    assert response.status_code == 200, response.text

    days = response.json()["days"]
    assert len(days) == 7
    for day in days:
        on_date = date.fromisoformat(day["date"])
        expected = 15 if on_date.weekday() >= 5 else 60
        assert day["slotMinutes"] == expected, day

    # The top-level value is documented as the *finest* across the window (task 3), never a
    # first-day-only value — the weekend's 15-minute grid is finer than the weekday's 60-minute
    # one, so it is what the top level reports.
    assert response.json()["slotMinutes"] == 15
