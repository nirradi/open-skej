"""Tests for ``GET /spaces/{public_id}/schedule`` (task 6.9).

Postgres-only and structured like ``test_rules_api.py``: ``get_current_user``
is overridden rather than exercised with a real token, and the whole module
skips when ``DATABASE_URL`` is unset. Fixtures are not shared across test
modules in this suite, so this file reproduces the same small fixture set
rather than importing it from ``test_rules_api``.

``app.rules_stub.resolve_day_schedule``'s own resolution logic — the
flat-AND combination of same-typed rows, the LCM of slot sizes, the
zero-width-window normalisation — is ``test_rules_stub.py``'s job. This
module covers the route's own contract: query-param validation, the
response shape for a real (seeded) Space, and this domain's usual
404-not-403 treatment for a caller outside the Space.
"""

import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.models import Base
from app.db.session import get_session
from app.identity import service
from app.identity.models import Space, SpaceRule, User
from app.main import app

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; the schedule API tests need `docker compose up -d`",
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
    """A ``TestClient`` with a swappable caller — see ``test_rules_api.py``."""

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
def space_a(session: Session, alice: User) -> Space:
    """Alice's Space. She is its owner and its only member.

    ``service.create_space`` seeds it with an unscoped 09:00-17:00
    ``availability_hours`` row and a 60-minute ``slot_alignment`` row (task
    6.6), so every date this Space's schedule is asked about resolves to
    that one ordinary configuration unless a test adds more rows.
    """
    return service.create_space(session, alice, name="Court A", description="Alice's court")


@pytest.fixture
def space_b(session: Session, bob: User) -> Space:
    """Bob's Space. Alice has no relationship with it whatsoever."""
    return service.create_space(session, bob, name="Court B", description="Bob's court")


# --- The ordinary case ---------------------------------------------------


def test_member_reads_the_seeded_default_schedule(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/schedule", params={"from": "2026-07-20", "days": 3}
    )

    assert response.status_code == 200
    body = response.json()
    assert [entry["date"] for entry in body] == ["2026-07-20", "2026-07-21", "2026-07-22"]
    for entry in body:
        assert entry["opens_at"] == "09:00:00"
        assert entry["closes_at"] == "17:00:00"
        assert entry["slot_minutes"] == 60
        assert entry["coherence_issue"] is None
        # The seeded Space holds no `min_duration` row (only `availability_hours` and
        # `slot_alignment`, `identity-and-access.md`), so this field resolves to `None` too.
        assert entry["min_duration_minutes"] is None


def test_min_duration_reaches_the_wire(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    session.add(
        SpaceRule(
            space_id=space_a.id,
            rule_type="min_duration",
            params={"min_duration_minutes": 45},
            applies_to=None,
            enabled=True,
        )
    )
    session.commit()

    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/schedule", params={"from": "2026-07-20", "days": 1}
    )

    assert response.status_code == 200
    entry = response.json()[0]
    assert entry["min_duration_minutes"] == 45


def test_a_space_with_no_rules_at_all_resolves_to_fully_unconfigured(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    for rule in session.execute(
        select(SpaceRule).where(SpaceRule.space_id == space_a.id)
    ).scalars():
        session.delete(rule)
    session.commit()

    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/schedule", params={"from": "2026-07-20", "days": 1}
    )

    assert response.status_code == 200
    entry = response.json()[0]
    assert entry["opens_at"] is None
    assert entry["closes_at"] is None
    assert entry["slot_minutes"] is None
    assert entry["coherence_issue"] is None
    assert entry["min_duration_minutes"] is None


# --- from/days validation --------------------------------------------------


def test_missing_from_is_422(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(f"/spaces/{space_a.public_id}/schedule", params={"days": 1})

    assert response.status_code == 422


def test_missing_days_is_422(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/schedule", params={"from": "2026-07-20"}
    )

    assert response.status_code == 422


def test_days_below_one_is_422(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/schedule", params={"from": "2026-07-20", "days": 0}
    )

    assert response.status_code == 422


def test_days_above_the_upper_bound_is_422(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/schedule", params={"from": "2026-07-20", "days": 63}
    )

    assert response.status_code == 422


def test_days_at_the_upper_bound_is_ok(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/schedule", params={"from": "2026-07-20", "days": 62}
    )

    assert response.status_code == 200
    assert len(response.json()) == 62


def test_malformed_from_is_422(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(
        f"/spaces/{space_a.public_id}/schedule", params={"from": "not-a-date", "days": 1}
    )

    assert response.status_code == 422


# --- Access control: member+, and this domain's usual 404-not-403 ----------


def test_a_stranger_gets_404_not_403(api: Api, alice: User, space_b: Space) -> None:
    """Alice has no relationship with Bob's Space at all."""
    response = api.as_user(alice).get(
        f"/spaces/{space_b.public_id}/schedule", params={"from": "2026-07-20", "days": 1}
    )

    assert response.status_code == 404


def test_a_nonexistent_public_id_gets_the_identical_404(api: Api, alice: User) -> None:
    response = api.as_user(alice).get(
        "/spaces/does-not-exist/schedule", params={"from": "2026-07-20", "days": 1}
    )

    assert response.status_code == 404
