"""Tests for the resource-scoped booking endpoints.

Postgres-only, following ``test_spaces_api.py`` and ``test_bookings_api.py``: the
whole module skips when ``DATABASE_URL`` is unset. Unlike ``test_bookings_api.py``
— which only needs the ``bookings`` table and the seeded default row — these
routes are authorized through ``require_space_role`` on a real Space, so the
fixtures here build the identity schema too: a Space (with its auto-created
Resource), a plain member, and a driver sharing the *same* engine as the identity
session. Two dependency overrides on one engine is the point: a booking created
through the API must be visible to a driver call made directly against the same
rows, and a Space/Resource looked up by the router must be the very row the test
set up.

``get_driver`` is overridden rather than left to build the process-wide,
``lru_cache``d driver — that cache is keyed for the life of the process, so an
un-overridden test would either build against the configured database or, worse,
share a driver instance across tests that each drop and recreate the schema.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth.dependencies import get_current_user
from app.db.models import Base, utcnow
from app.db.postgres import PostgresBookingDriver
from app.db.session import get_session
from app.dependencies import get_driver
from app.identity import service
from app.identity.models import MembershipRole, Resource, Space, SpaceMembership, User
from app.main import app

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; the resource-scoped booking routes need `docker compose up -d`",
)

# Tomorrow, not a fixed date — the endpoint calls the real ``evaluate()`` with no
# pinned ``now``, so a hardcoded date would start failing as "already passed" the
# day it went by. Mirrors ``test_bookings_api.py``.
DAY = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
    hour=0, minute=0, second=0, microsecond=0
)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


def iso(value: datetime) -> str:
    return value.isoformat()


# --- Fixtures. ----------------------------------------------------------------


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
    """One session shared by the tests and the app under test.

    ``expire_on_commit=False`` because the service layer commits, and a test that
    inspected an ORM object afterwards would otherwise trigger a refresh against a
    session FastAPI has moved on from.
    """
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def driver(engine) -> PostgresBookingDriver:
    """A driver bound to the *same* engine the identity session uses.

    A booking created through the API must be readable back by a direct driver
    call in the same test — and a booking inserted directly via the driver (the
    no-cancel-after-start probe) must be visible to the router — which only holds
    if both point at one schema.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return PostgresBookingDriver(factory)


class Api:
    """A ``TestClient`` with a swappable caller. See ``test_spaces_api.py``."""

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
    """A Space with its auto-created first Resource, owned by ``owner``."""
    return service.create_space(session, owner, name="Court Club", description="A club")


@pytest.fixture
def resource(session: Session, space: Space) -> Resource:
    return session.execute(select(Resource).where(Resource.space_id == space.id)).scalar_one()


@pytest.fixture(autouse=True)
def _add_member(session: Session, space: Space, member: User) -> None:
    session.add(SpaceMembership(space_id=space.id, user_id=member.id, role=MembershipRole.MEMBER))
    session.commit()


def _url(space: Space, resource: Resource, suffix: str = "") -> str:
    return f"/spaces/{space.public_id}/resources/{resource.id}/bookings{suffix}"


# --- Create. -------------------------------------------------------------------


def test_owner_can_create_a_booking(
    api: Api, owner: User, space: Space, resource: Resource
) -> None:
    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user_id"] == owner.id
    assert body["resource_id"] == resource.id
    assert body["status"] == "confirmed"


def test_a_plain_member_can_create_a_booking(
    api: Api, member: User, space: Space, resource: Resource
) -> None:
    """Membership and roles stay at the Space; any member may book any Resource."""
    response = api.as_user(member).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user_id"] == member.id
    assert body["resource_id"] == resource.id


def test_created_booking_carries_the_real_caller_and_resource(
    api: Api,
    session: Session,
    driver: PostgresBookingDriver,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    """Asserted against the persisted row, not just the echoed response."""
    created = (
        api.as_user(owner)
        .post(_url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))})
        .json()
    )

    stored = driver.get_booking(created["id"])
    assert stored.user_id == owner.id
    assert stored.resource_id == resource.id


# --- Rule denial and overlap. ---------------------------------------------------


def test_rule_denial_returns_422_and_persists_nothing(
    api: Api, driver: PostgresBookingDriver, owner: User, space: Space, resource: Resource
) -> None:
    """A 3-hour booking trips the canon's max-duration rule."""
    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(13))}
    )

    assert response.status_code == 422
    assert response.json()["error"] == "rule_denied"

    bookings = driver.list_bookings(
        start=DAY - timedelta(days=1),
        end=DAY + timedelta(days=1),
        resource_id=resource.id,
        include_cancelled=True,
    )
    assert bookings == []


def test_overlapping_booking_returns_409(
    api: Api, owner: User, space: Space, resource: Resource
) -> None:
    first = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))}
    )
    assert first.status_code == 201

    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10, 30)), "end_at": iso(at(12))}
    )

    assert response.status_code == 409
    assert response.json()["error"] == "overlap"


# --- Archived Space: create refused, reads and cancels still work. -------------


def test_archived_space_rejects_create_with_409_and_persists_nothing(
    api: Api, driver: PostgresBookingDriver, owner: User, space: Space, resource: Resource
) -> None:
    assert api.as_user(owner).post(f"/spaces/{space.public_id}/archive").status_code == 200

    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))}
    )

    assert response.status_code == 409
    assert response.json()["error"] == "space_archived"

    bookings = driver.list_bookings(
        start=DAY - timedelta(days=1),
        end=DAY + timedelta(days=1),
        resource_id=resource.id,
        include_cancelled=True,
    )
    assert bookings == []


def test_archived_check_runs_before_the_rules_or_driver_are_touched(
    api: Api, owner: User, space: Space, resource: Resource
) -> None:
    """Pins the ordering directly, mirroring ``test_rules_run_before_the_driver_is_touched``.

    A 3-hour booking would also trip the rule engine, so this proves the archived
    check is refused first and neither ``evaluate`` nor the driver is ever reached
    — not merely that the outcome happens to be 409 either way.
    """
    assert api.as_user(owner).post(f"/spaces/{space.public_id}/archive").status_code == 200

    import app.routers.resource_bookings as resource_bookings_module

    def exploding_evaluate(_request):
        raise AssertionError("the rule engine must not run against an archived Space")

    original_evaluate = resource_bookings_module.evaluate
    resource_bookings_module.evaluate = exploding_evaluate
    try:
        response = api.as_user(owner).post(
            _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(13))}
        )
    finally:
        resource_bookings_module.evaluate = original_evaluate

    assert response.status_code == 409
    assert response.json()["error"] == "space_archived"


def test_archived_space_still_lists_bookings(
    api: Api, driver: PostgresBookingDriver, owner: User, space: Space, resource: Resource
) -> None:
    driver.create_booking(start_at=at(10), end_at=at(11), user_id=owner.id, resource_id=resource.id)
    api.as_user(owner).post(f"/spaces/{space.public_id}/archive")

    response = api.as_user(owner).get(
        _url(space, resource), params={"from": iso(at(0)), "to": iso(at(23))}
    )

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_archived_space_still_allows_cancelling_a_future_booking(
    api: Api, driver: PostgresBookingDriver, owner: User, space: Space, resource: Resource
) -> None:
    booking = driver.create_booking(
        start_at=at(10), end_at=at(11), user_id=owner.id, resource_id=resource.id
    )
    api.as_user(owner).post(f"/spaces/{space.public_id}/archive")

    response = api.as_user(owner).delete(_url(space, resource, f"/{booking.id}"))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


# --- Listing. --------------------------------------------------------------------


def test_get_scopes_to_this_resource_only(
    api: Api,
    session: Session,
    driver: PostgresBookingDriver,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    """A booking on another Resource of the same Space is not returned."""
    other = service.create_resource(session, space, name="Court 2")

    mine = driver.create_booking(
        start_at=at(10), end_at=at(11), user_id=owner.id, resource_id=resource.id
    )
    driver.create_booking(start_at=at(10), end_at=at(11), user_id=owner.id, resource_id=other.id)

    response = api.as_user(owner).get(
        _url(space, resource), params={"from": iso(at(0)), "to": iso(at(23))}
    )

    assert response.status_code == 200
    assert [b["id"] for b in response.json()] == [mine.id]


# --- Cancel. -----------------------------------------------------------------------


def test_cancel_a_future_booking_returns_200(
    api: Api, owner: User, space: Space, resource: Resource
) -> None:
    created = (
        api.as_user(owner)
        .post(_url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))})
        .json()
    )

    response = api.as_user(owner).delete(_url(space, resource, f"/{created['id']}"))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_cancelling_an_already_started_booking_returns_409_and_leaves_it_confirmed(
    api: Api, driver: PostgresBookingDriver, owner: User, space: Space, resource: Resource
) -> None:
    """Inserted directly via the driver, bypassing the create route's own
    ``NotInThePastRule`` — the only way to get a past booking onto the calendar at
    all.
    """
    past = driver.create_booking(
        start_at=utcnow() - timedelta(hours=1),
        end_at=utcnow() + timedelta(minutes=30),
        user_id=owner.id,
        resource_id=resource.id,
    )

    response = api.as_user(owner).delete(_url(space, resource, f"/{past.id}"))

    assert response.status_code == 409
    assert response.json()["error"] == "already_started"

    stored = driver.get_booking(past.id)
    assert stored.status == "confirmed"


def test_cancelling_a_booking_of_another_resource_is_404(
    api: Api,
    session: Session,
    driver: PostgresBookingDriver,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    other = service.create_resource(session, space, name="Court 2")
    foreign = driver.create_booking(
        start_at=at(10), end_at=at(11), user_id=owner.id, resource_id=other.id
    )

    response = api.as_user(owner).delete(_url(space, resource, f"/{foreign.id}"))

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_cancelling_a_nonexistent_booking_is_404(
    api: Api, owner: User, space: Space, resource: Resource
) -> None:
    response = api.as_user(owner).delete(_url(space, resource, "/999999"))

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_cancelling_an_already_cancelled_booking_is_409(
    api: Api, owner: User, space: Space, resource: Resource
) -> None:
    created = (
        api.as_user(owner)
        .post(_url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))})
        .json()
    )
    assert api.as_user(owner).delete(_url(space, resource, f"/{created['id']}")).status_code == 200

    response = api.as_user(owner).delete(_url(space, resource, f"/{created['id']}"))

    assert response.status_code == 409
    assert response.json()["error"] == "already_cancelled"


# --- Non-member: focused case, on top of the isolation sweep. -------------------


@pytest.fixture
def stranger(session: Session) -> User:
    """A user with no membership row in ``space`` at all — not even ``member``."""
    return _make_user(session, "auth0|stranger", "stranger@example.com")


def test_a_non_member_gets_404_not_403(
    api: Api, stranger: User, space: Space, resource: Resource
) -> None:
    """Named directly rather than left to the sweep in ``test_spaces_api.py``.

    That sweep already covers every route under ``/spaces/{public_id}``,
    including these; this test states the rule for this module too, so a failure
    here reads as "the resource-scoped routes leaked" rather than one row of a
    parametrised table elsewhere.
    """
    response = api.as_user(stranger).get(
        _url(space, resource), params={"from": iso(at(0)), "to": iso(at(23))}
    )

    assert response.status_code == 404
    assert response.status_code != 403
