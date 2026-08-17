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
from zoneinfo import ZoneInfo

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
from app.identity.models import (
    MembershipRole,
    Resource,
    ShapeStatus,
    Space,
    SpaceCalendarShape,
    SpaceMembership,
    User,
)
from app.identity.schemas import SpaceRuleUpdate
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


# A shape permissive enough that the pre-existing rule-engine tests below (max_duration,
# session_length, overlap) are refused only by the *rule* each is named for, never by the
# availability gate (task 10.3) that now runs ahead of it. ``DEFAULT_SHAPE``'s own [60]-only grid
# would otherwise refuse several of their 30- and 120-minute bookings before the rule under test
# ever ran, which is exactly the ordering bug this gate must not introduce into an unrelated suite.
_PERMISSIVE_SHAPE = {
    "version": 1,
    "operating_blocks": [
        {
            "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
            "start_time": "00:00",
            "end_time": "24:00",
            "allowed_durations_mins": [30, 60, 90, 120, 180],
        }
    ],
    "blackout_windows": [],
}


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


def _set_rule(session: Session, space: Space, rule_type: str, params: dict) -> None:
    """Set this Space's one unscoped instance of ``rule_type`` to ``params``.

    ``create_space`` seeds an ``availability_hours`` and a ``session_length``
    row, so a test tightening either has to edit the row it finds rather than
    add a second instance: two instances of a type both run and AND to the
    stricter, and the seeded 60-minute grid would go on refusing a booking
    aligned only to a 30-minute one.
    """
    existing = next(
        (
            rule
            for rule in service.list_space_rules(session, space)
            if rule.rule_type == rule_type and rule.applies_to is None
        ),
        None,
    )
    if existing is None:
        service.create_space_rule(
            session, space, rule_type=rule_type, params=params, applies_to=None, enabled=True
        )
    else:
        service.update_space_rule(
            session, space, rule_id=existing.id, payload=SpaceRuleUpdate(params=params)
        )


def _set_shape(session: Session, space: Space, owner: User, document: dict) -> None:
    """Publish ``document`` as this Space's live shape, replacing whatever came before.

    Goes through the real service functions (``upsert_draft`` then ``publish_draft``) rather than
    writing the row directly, so a test using this exercises the same validated write path a real
    admin's chat turn would, not a shortcut around it.
    """
    service.upsert_draft(session, space, document, owner)
    service.publish_draft(session, space, owner)


@pytest.fixture
def space(session: Session, owner: User) -> Space:
    """A Space with its auto-created first Resource, owned by ``owner``.

    A ``max_duration`` rule is added explicitly (``create_space`` seeds only
    operating hours and a slot grid) so the canon this module's tests exercise
    actually includes a duration cap — the per-Space canon assembled for a
    Space with no configuration would enforce nothing but ``NotInThePastRule``,
    and ``test_rule_denial_returns_422_and_persists_nothing`` needs a real rule
    to trip.

    Its live shape is replaced with ``_PERMISSIVE_SHAPE`` (task 10.3): the availability gate now
    runs ahead of every rule this module tests, and ``create_space``'s own ``DEFAULT_SHAPE`` only
    offers 60-minute bookings — too narrow for the 30-, 90- and 120-minute requests several of
    those rule tests depend on to reach the rule engine at all.
    """
    space = service.create_space(session, owner, name="Court Club", description="A club")
    _set_rule(session, space, "max_duration", {"max_duration_minutes": 120})
    _set_shape(session, space, owner, _PERMISSIVE_SHAPE)
    return space


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
    assert body["mine"] is True
    assert body["resource_id"] == resource.id
    assert body["status"] == "confirmed"


def test_a_plain_member_can_create_a_booking(
    api: Api, member: User, space: Space, resource: Resource
) -> None:
    """Membership and roles stay at the Space; any member may book any Resource.

    ``user_id`` is ``None`` even though this is the member's own booking:
    visibility of the owner is gated on the caller's role, not on whose
    booking it is. ``mine`` is what answers "is this mine" for a plain member.
    """
    response = api.as_user(member).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user_id"] is None
    assert body["mine"] is True
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
    """A partial overlap, both ends on ``space``'s (default 60-minute) grid.

    The second attempt used to start at a half-hour offset — off-grid now that
    ``session_minutes`` is enforced (task 6.5) — which would trip the new
    ``SessionLengthRule`` before the overlap constraint ever ran. 10:00-12:00
    partially overlaps the first booking's 10:00-11:00 just as well and stays
    on the grid.
    """
    first = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))}
    )
    assert first.status_code == 201

    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(12))}
    )

    assert response.status_code == 409
    assert response.json()["error"] == "overlap"


# --- Session length (task 6.5) --------------------------------------------------


def test_off_grid_booking_is_refused_by_session_length(
    api: Api,
    driver: PostgresBookingDriver,
    session: Session,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    """This exact request succeeds today; the point of task 6.5 is that it no longer does.

    ``session_minutes`` used to decline to *offer* an off-grid slot in the calendar UI while the
    API accepted anything — the split ``rule-engine.md`` warns is only safe as long as the grid is
    advisory and something else is the real boundary. Nothing enforced it server-side until now.
    """
    _set_rule(session, space, "session_length", {"session_minutes": 30})

    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10, 7)), "end_at": iso(at(10, 22))}
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


def test_an_on_grid_booking_is_unaffected_by_session_length(
    api: Api, session: Session, owner: User, space: Space, resource: Resource
) -> None:
    """The new rule only refuses what is actually off-grid."""
    _set_rule(session, space, "session_length", {"session_minutes": 30})

    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10, 30)), "end_at": iso(at(11))}
    )

    assert response.status_code == 201, response.text


# --- The availability shape gate (task 10.3). -----------------------------------
#
# ``space``'s own fixture already proves the ordinary case — every test above that reaches 201
# passed the (permissive) shape gate and then the rule engine. These tests exercise the gate's
# own refusals, each against a deliberately narrower shape than the fixture's default.

_NARROW_SHAPE = {
    "version": 1,
    "operating_blocks": [
        {
            "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
            "start_time": "09:00",
            "end_time": "17:00",
            "allowed_durations_mins": [60],
        }
    ],
    "blackout_windows": [],
}


def test_a_booking_outside_the_shape_is_refused_and_never_reaches_the_rule_engine(
    api: Api,
    driver: PostgresBookingDriver,
    session: Session,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    """Proves the ordering directly, mirroring
    ``test_archived_check_runs_before_the_rules_or_driver_are_touched``: the gate must refuse
    *before* ``evaluate`` is ever called, not merely produce the same outcome it would have.
    """
    _set_shape(session, space, owner, _NARROW_SHAPE)

    import app.routers.resource_bookings as resource_bookings_module

    def exploding_evaluate(*args, **kwargs):
        raise AssertionError("the rule engine must not run for a booking the shape already refused")

    original_evaluate = resource_bookings_module.evaluate
    resource_bookings_module.evaluate = exploding_evaluate
    try:
        # 20:00 is well outside the block's 09:00-17:00 window.
        response = api.as_user(owner).post(
            _url(space, resource), json={"start_at": iso(at(20)), "end_at": iso(at(21))}
        )
    finally:
        resource_bookings_module.evaluate = original_evaluate

    assert response.status_code == 422
    assert response.json()["error"] == "rule_denied"

    bookings = driver.list_bookings(
        start=DAY - timedelta(days=1),
        end=DAY + timedelta(days=1),
        resource_id=resource.id,
        include_cancelled=True,
    )
    assert bookings == []


def test_a_duration_the_block_does_not_offer_is_refused(
    api: Api,
    driver: PostgresBookingDriver,
    session: Session,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    _set_shape(session, space, owner, _NARROW_SHAPE)

    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11, 30))}
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


def test_a_start_off_the_blocks_grid_is_refused(
    api: Api,
    driver: PostgresBookingDriver,
    session: Session,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    _set_shape(session, space, owner, _NARROW_SHAPE)

    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10, 15)), "end_at": iso(at(11, 15))}
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


def test_a_booking_overlapping_a_blackout_is_refused(
    api: Api,
    driver: PostgresBookingDriver,
    session: Session,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    _set_shape(
        session,
        space,
        owner,
        {
            "version": 1,
            "operating_blocks": [
                {
                    "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                    "start_time": "09:00",
                    "end_time": "17:00",
                    "allowed_durations_mins": [60],
                }
            ],
            "blackout_windows": [
                {"start_time": "10:00", "end_time": "11:00", "reason": "Court maintenance"}
            ],
        },
    )

    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))}
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


def test_an_unreadable_stored_shape_refuses_the_booking(
    api: Api,
    driver: PostgresBookingDriver,
    session: Session,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    """``live_shape`` re-validates on every read (task 10.2) — a document written before a schema
    change is a document nobody re-checked, and the gate must refuse rather than fall back to
    anything permissive when that happens (fail closed, ``.claude/rules/calendar-shape.md``)."""
    live_row = session.execute(
        select(SpaceCalendarShape).where(
            SpaceCalendarShape.space_id == space.id,
            SpaceCalendarShape.status == ShapeStatus.LIVE,
        )
    ).scalar_one()
    live_row.document = {
        "version": 1,
        "operating_blocks": [
            # `days: []` fails `validate_shape` outright — an operating block naming no day at
            # all, the same broken document `test_space_calendar_shapes.py` uses.
            {"days": [], "start_time": "09:00", "end_time": "17:00", "allowed_durations_mins": [60]}
        ],
        "blackout_windows": [],
    }
    session.commit()

    response = api.as_user(owner).post(
        _url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))}
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


def test_cancellation_of_a_booking_now_outside_the_shape_still_succeeds(
    api: Api, session: Session, owner: User, space: Space, resource: Resource
) -> None:
    """A member trapped with an uncancellable booking is the direct cost of getting this wrong
    (``.claude/rules/calendar-shape.md``) — publishing a shape that no longer covers an existing
    booking must never block cancelling it."""
    created = (
        api.as_user(owner)
        .post(_url(space, resource), json={"start_at": iso(at(10)), "end_at": iso(at(11))})
        .json()
    )

    # Close the venue entirely — the booking above now sits outside the live shape.
    _set_shape(
        session, space, owner, {"version": 1, "operating_blocks": [], "blackout_windows": []}
    )

    response = api.as_user(owner).delete(_url(space, resource, f"/{created['id']}"))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_a_sydney_space_resolves_the_shape_against_its_own_local_date(
    api: Api, session: Session, owner: User
) -> None:
    """The gate converts at the boundary in the Space's own zone
    (``.claude/rules/calendar-shape.md``), never UTC's — a booking whose local date and UTC date
    disagree must be judged against the *local* one, exactly as ``rules_stub`` already does for
    the rule engine's own local frame.

    A fresh Space rather than the shared ``space`` fixture, because its timezone has to be set
    before anything is asked about a local date on it.
    """
    space = service.create_space(session, owner, name="Sydney Club", description=None)
    space.timezone = "Australia/Sydney"
    session.commit()
    resource = session.execute(select(Resource).where(Resource.space_id == space.id)).scalar_one()

    sydney = ZoneInfo("Australia/Sydney")
    local_date = (datetime.now(sydney) + timedelta(days=30)).date()
    _set_shape(
        session,
        space,
        owner,
        {
            "version": 1,
            "operating_blocks": [
                {
                    "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                    "start_time": "00:00",
                    "end_time": "24:00",
                    "allowed_durations_mins": [60],
                    "effective_from": local_date.isoformat(),
                    "effective_to": local_date.isoformat(),
                }
            ],
            "blackout_windows": [],
        },
    )

    # 09:00 local is inside the seeded 09:00-17:00 `availability_hours` row too (this fresh Space
    # keeps `create_space`'s own seeded rules), so a pass here is the shape and the engine
    # agreeing, not one of them happening not to run.
    local_start = datetime(local_date.year, local_date.month, local_date.day, 9, 0, tzinfo=sydney)
    local_end = local_start + timedelta(hours=1)
    # Sanity: the instant this books falls on the *previous* UTC date, so a gate that resolved the
    # UTC date instead of the local one would find no operating block covering it at all — the
    # block is scoped to `local_date` alone via `effective_from`/`effective_to`.
    assert local_start.astimezone(timezone.utc).date() != local_date

    response = api.as_user(owner).post(
        f"/spaces/{space.public_id}/resources/{resource.id}/bookings",
        json={"start_at": local_start.isoformat(), "end_at": local_end.isoformat()},
    )

    assert response.status_code == 201, response.text


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


def test_a_plain_member_listing_the_week_sees_no_user_ids_but_correct_mine(
    api: Api,
    driver: PostgresBookingDriver,
    member: User,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    """``user_id`` never reaches a plain member, whoever the booking belongs to;
    ``mine`` is what tells them theirs from anyone else's.
    """
    driver.create_booking(
        start_at=at(10), end_at=at(11), user_id=member.id, resource_id=resource.id
    )
    driver.create_booking(start_at=at(12), end_at=at(13), user_id=owner.id, resource_id=resource.id)

    response = api.as_user(member).get(
        _url(space, resource), params={"from": iso(at(0)), "to": iso(at(23))}
    )

    assert response.status_code == 200
    body = {b["mine"]: b for b in response.json()}
    assert set(body) == {True, False}
    assert body[True]["user_id"] is None
    assert body[False]["user_id"] is None


def test_admin_and_owner_listing_the_week_see_user_ids(
    api: Api,
    driver: PostgresBookingDriver,
    member: User,
    admin: User,
    owner: User,
    space: Space,
    resource: Resource,
) -> None:
    driver.create_booking(
        start_at=at(10), end_at=at(11), user_id=member.id, resource_id=resource.id
    )

    for caller in (admin, owner):
        response = api.as_user(caller).get(
            _url(space, resource), params={"from": iso(at(0)), "to": iso(at(23))}
        )
        assert response.status_code == 200
        [body] = response.json()
        assert body["user_id"] == member.id
        assert body["mine"] is False


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


# --- Ownership: a member cancels their own; admin and owner cancel any. --------


@pytest.fixture
def other_member(session: Session) -> User:
    """A second plain member of ``space``, distinct from ``member``.

    Needed because "another member's booking" requires two members who are
    each other's "someone else" — cancelling ``owner``'s booking would also
    prove the point, but conflating it with the owner risks reading a pass here
    as evidence for the wrong rule (rank above admin) rather than the one this
    test is actually about (rank below admin).
    """
    return _make_user(session, "auth0|other-member", "other-member@example.com")


@pytest.fixture
def admin(session: Session, space: Space) -> User:
    user = _make_user(session, "auth0|admin", "admin@example.com")
    session.add(SpaceMembership(space_id=space.id, user_id=user.id, role=MembershipRole.ADMIN))
    session.commit()
    return user


def test_a_member_cancelling_another_members_booking_is_refused_and_the_booking_survives(
    api: Api,
    session: Session,
    driver: PostgresBookingDriver,
    member: User,
    other_member: User,
    space: Space,
    resource: Resource,
) -> None:
    """The defect this task fixes: three guards ran before the release — proving
    membership, that the booking is on this Resource, and that it has not
    started — and not one of them compared ``booking.user_id`` to the caller.

    Asserts the row is still ``confirmed``, not merely that the response was
    403 — a check that returns the right status while still cancelling the
    booking underneath it is the failure this guards against.
    """
    session.add(
        SpaceMembership(space_id=space.id, user_id=other_member.id, role=MembershipRole.MEMBER)
    )
    session.commit()
    booking = driver.create_booking(
        start_at=at(10), end_at=at(11), user_id=member.id, resource_id=resource.id
    )

    response = api.as_user(other_member).delete(_url(space, resource, f"/{booking.id}"))

    assert response.status_code == 403
    assert response.json()["error"] == "not_yours"

    stored = driver.get_booking(booking.id)
    assert stored.status == "confirmed"


def test_an_admin_can_cancel_a_members_booking(
    api: Api,
    driver: PostgresBookingDriver,
    member: User,
    admin: User,
    space: Space,
    resource: Resource,
) -> None:
    booking = driver.create_booking(
        start_at=at(10), end_at=at(11), user_id=member.id, resource_id=resource.id
    )

    response = api.as_user(admin).delete(_url(space, resource, f"/{booking.id}"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["mine"] is False
    assert body["user_id"] == member.id


def test_the_owner_can_cancel_a_members_booking(
    api: Api,
    driver: PostgresBookingDriver,
    owner: User,
    member: User,
    space: Space,
    resource: Resource,
) -> None:
    """The owner ranks above admin in ``_ROLE_RANK``, so the same ladder covers
    it without a separate check.
    """
    booking = driver.create_booking(
        start_at=at(10), end_at=at(11), user_id=member.id, resource_id=resource.id
    )

    response = api.as_user(owner).delete(_url(space, resource, f"/{booking.id}"))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_a_member_can_cancel_their_own_booking(
    api: Api, driver: PostgresBookingDriver, member: User, space: Space, resource: Resource
) -> None:
    booking = driver.create_booking(
        start_at=at(10), end_at=at(11), user_id=member.id, resource_id=resource.id
    )

    response = api.as_user(member).delete(_url(space, resource, f"/{booking.id}"))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


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
