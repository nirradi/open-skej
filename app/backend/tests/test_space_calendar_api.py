"""Tests for ``GET /spaces/{public_id}/calendar`` (task 10.3).

Postgres-only. This is the one endpoint the calendar grid reads its layout from -- the retired
``GET /spaces/{public_id}/schedule`` and its ``resolve_day_schedule`` resolution
(``.claude/rules/calendar-shape.md``, "Two rule types this document replaced") answered the
identical question over rule rows; this route answers it from the Space's own calendar shape
instead. Fixtures are not shared across test modules in this suite, so this file builds its own
small set rather than importing one.

``rules/shape/projection.py``'s own resolution logic -- the grid, the blackout truncation, the
union of overlapping blocks -- is ``rules/tests``'s job (task 10.1). This module covers the route's
own contract: query-param validation, the range cap, the two roles on one route (member+ for the
live shape, admin+ for ``draft=true``), and this domain's usual 404-not-403 treatment for a caller
outside the Space.
"""

import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.models import Base
from app.db.session import get_session
from app.identity import service
from app.identity.models import MembershipRole, Space, SpaceMembership, User
from app.main import app

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; the calendar API tests need `docker compose up -d`",
)


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


class Api:
    """A ``TestClient`` with a swappable caller -- see ``test_space_schedule_api.py``."""

    def __init__(self, client: TestClient, caller: dict[str, User]) -> None:
        self._client = client
        self._caller = caller

    def as_user(self, user: User) -> TestClient:
        self._caller["user"] = user
        return self._client


@pytest.fixture
def api(session: Session) -> Iterator[Api]:
    caller: dict[str, User] = {}

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: caller["user"]
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
def alice(session: Session) -> User:
    return _make_user(session, "auth0|alice", "alice@example.com")


@pytest.fixture
def bob(session: Session) -> User:
    return _make_user(session, "auth0|bob", "bob@example.com")


@pytest.fixture
def carol(session: Session) -> User:
    """A plain member of ``space_a`` -- distinct from ``alice``, its owner, so the draft's
    admin-only floor can be shown to refuse someone below it and not just an unrelated stranger."""
    return _make_user(session, "auth0|carol", "carol@example.com")


@pytest.fixture
def space_a(session: Session, alice: User, carol: User) -> Space:
    """Alice's Space. She is its owner; Carol is a plain member.

    ``service.create_space`` writes a live ``DEFAULT_SHAPE`` row (task 10.2) -- this Space is never
    asked about with no live shape row at all, matching the invariant
    ``.claude/rules/calendar-shape.md`` states.
    """
    space = service.create_space(session, alice, name="Court A", description="Alice's court")
    session.add(SpaceMembership(space_id=space.id, user_id=carol.id, role=MembershipRole.MEMBER))
    session.commit()
    return space


@pytest.fixture
def space_b(session: Session, bob: User) -> Space:
    """Bob's Space. Alice has no relationship with it whatsoever."""
    return service.create_space(session, bob, name="Court B", description="Bob's court")


# --- The ordinary case: the live shape's own projection. -----------------------


def test_member_reads_the_default_live_shapes_projection(
    api: Api, alice: User, space_a: Space
) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "2026-07-20", "to": "2026-07-21"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [entry["date"] for entry in body] == ["2026-07-20", "2026-07-21"]

    entry = body[0]
    assert entry["bookable"] is True
    assert len(entry["offered_starts"]) == 24
    assert entry["offered_starts"][0] == {"start_minutes": 0, "durations_mins": [60]}
    assert entry["blackout_intervals"] == []
    assert entry["operating_intervals"] == [
        {"start_minutes": 0, "end_minutes": 1440, "allowed_durations_mins": [60]}
    ]


def test_a_plain_member_reads_the_same_projection_as_the_owner(
    api: Api, carol: User, space_a: Space
) -> None:
    response = api.as_user(carol).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "2026-07-20", "to": "2026-07-20"},
    )

    assert response.status_code == 200
    assert response.json()[0]["bookable"] is True


# --- from/to validation. ----------------------------------------------------


def test_to_before_from_is_400(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "2026-07-20", "to": "2026-07-19"},
    )

    assert response.status_code == 400


def test_range_above_the_upper_bound_is_400(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "2026-07-20", "to": "2026-09-21"},  # 63 days inclusive
    )

    assert response.status_code == 400


def test_range_at_the_upper_bound_is_ok(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "2026-07-20", "to": "2026-09-19"},  # 62 days inclusive
    )

    assert response.status_code == 200
    assert len(response.json()) == 62


def test_a_single_day_range_is_inclusive_on_both_ends(
    api: Api, alice: User, space_a: Space
) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "2026-07-20", "to": "2026-07-20"},
    )

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_malformed_from_is_422(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "not-a-date", "to": "2026-07-20"},
    )

    assert response.status_code == 422


# --- The draft, and its own stricter role. ----------------------------------


def test_a_plain_member_requesting_the_draft_is_403(api: Api, carol: User, space_a: Space) -> None:
    response = api.as_user(carol).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "2026-07-20", "to": "2026-07-20", "draft": "true"},
    )

    assert response.status_code == 403


def test_admin_requesting_the_draft_with_none_written_is_404(
    api: Api, alice: User, space_a: Space
) -> None:
    """Alice is the owner, ranked above admin, so this also proves the floor is admin+ rather
    than owner-only -- and that no draft exists for ``create_space``'s own live row to be mistaken
    for."""
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "2026-07-20", "to": "2026-07-20", "draft": "true"},
    )

    assert response.status_code == 404


def test_admin_sees_the_drafts_own_content_distinct_from_live(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    draft_document = {
        "version": 1,
        "operating_blocks": [
            {
                "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                "start_time": "00:00",
                "end_time": "24:00",
                "allowed_durations_mins": [45],
            }
        ],
        "blackout_windows": [],
    }
    service.upsert_draft(session, space_a, draft_document, alice)

    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar",
        params={"from": "2026-07-20", "to": "2026-07-20", "draft": "true"},
    )

    assert response.status_code == 200
    assert response.json()[0]["offered_starts"][0]["durations_mins"] == [45]

    # The live shape is untouched -- previewing a draft is not the same as publishing it.
    live_response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/calendar", params={"from": "2026-07-20", "to": "2026-07-20"}
    )
    assert live_response.json()[0]["offered_starts"][0]["durations_mins"] == [60]


# --- Access control: member+, and this domain's usual 404-not-403. ----------


def test_a_stranger_gets_404_not_403(api: Api, alice: User, space_b: Space) -> None:
    """Alice has no relationship with Bob's Space at all."""
    response = api.as_user(alice).get(
        f"/spaces/{space_b.public_id}/calendar",
        params={"from": "2026-07-20", "to": "2026-07-20"},
    )

    assert response.status_code == 404


def test_a_nonexistent_public_id_gets_the_identical_404(api: Api, alice: User) -> None:
    response = api.as_user(alice).get(
        "/spaces/does-not-exist/calendar", params={"from": "2026-07-20", "to": "2026-07-20"}
    )

    assert response.status_code == 404
