"""Tests for the rules API (task 6.7): ``GET /rule-types`` and the
``/spaces/{public_id}/rules`` CRUD onto ``space_rules`` rows.

Postgres-only and structured like ``test_spaces_api.py``: ``get_current_user``
is overridden rather than exercised with a real token, and the whole module
skips when ``DATABASE_URL`` is unset. Fixtures are not shared across test
modules in this suite (only ``conftest.py``'s Postgres/``btree_gist`` plumbing
is), so this file reproduces the same small fixture set rather than importing
it from ``test_spaces_api``.

Role-gate enforcement for the four space-scoped routes added here
(``GET``/``POST /rules``, ``PATCH``/``DELETE /rules/{rule_id}``) is proven by
``test_spaces_api.py``'s ``ROLE_TABLE``-driven sweep, not duplicated here —
this module covers the request/response contract and the validation rules
that sweep does not exercise.
"""

import os
from typing import Any, Iterator

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
    reason="DATABASE_URL is unset; the rules API tests need `docker compose up -d`",
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
    """A ``TestClient`` with a swappable caller — see ``test_spaces_api.py``."""

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
    """Alice's Space. She is its owner and its only member."""
    return service.create_space(session, alice, name="Court A", description="Alice's court")


@pytest.fixture
def space_b(session: Session, bob: User) -> Space:
    """Bob's Space. Alice has no relationship with it whatsoever."""
    return service.create_space(session, bob, name="Court B", description="Bob's court")


def _add_member(session: Session, space: Space, user: User, role: MembershipRole) -> None:
    session.add(SpaceMembership(space_id=space.id, user_id=user.id, role=role))
    session.commit()


# --- GET /rule-types ----------------------------------------------------------


def test_rule_types_lists_every_registered_type_in_priority_order(api: Api, alice: User) -> None:
    response = api.as_user(alice).get("/rule-types")

    assert response.status_code == 200
    body = response.json()

    rule_type_ids = [entry["rule_type"] for entry in body]
    # `session_length` (35) and `availability_hours` (40) used to sit between
    # `max_consecutive_duration` and `max_duration_per_day`. Both were retired
    # with the calendar shape (task 10.5) and their priorities are deliberately
    # left unused rather than closed up — renumbering would silently change
    # which denial a member reads (`.claude/rules/rule-engine.md`).
    assert rule_type_ids == [
        "not_in_the_past",
        "booking_horizon",
        "max_duration",
        "max_consecutive_duration",
        "max_duration_per_day",
        "max_bookings_per_day",
        "max_bookings_per_week",
        "max_bookings_per_month",
    ]

    priorities = [entry["priority"] for entry in body]
    assert priorities == sorted(priorities)

    max_duration_entry = next(entry for entry in body if entry["rule_type"] == "max_duration")
    assert max_duration_entry["params"] == [
        {
            "name": "max_duration_minutes",
            "kind": "integer",
            "label": "Maximum duration",
            "unit": "minutes",
            "required": True,
            "minimum": 1,
        }
    ]
    assert max_duration_entry["is_single"] is False
    assert max_duration_entry["reads_history"] is False

    max_consecutive_duration_entry = next(
        entry for entry in body if entry["rule_type"] == "max_consecutive_duration"
    )
    assert max_consecutive_duration_entry["params"] == [
        {
            "name": "max_consecutive_minutes",
            "kind": "integer",
            "label": "Maximum consecutive duration",
            "unit": "minutes",
            "required": True,
            "minimum": 1,
        }
    ]
    assert max_consecutive_duration_entry["is_single"] is False
    # This is the property the whole task turns on: `evaluate` never names `context.history`, but
    # the run it reads is resolved from history, so the router must still be told to load it.
    assert max_consecutive_duration_entry["reads_history"] is True

    not_in_the_past_entry = next(entry for entry in body if entry["rule_type"] == "not_in_the_past")
    assert not_in_the_past_entry["params"] == []


def test_rule_types_serve_a_description_for_all_eight_hand_written_types(
    api: Api, alice: User
) -> None:
    """A picker where some entries explain themselves and others do not is worse than one where
    none do — every hand-written type carries a non-empty description over the wire."""
    body = api.as_user(alice).get("/rule-types").json()

    assert len(body) == 8
    for entry in body:
        assert isinstance(entry["description"], str)
        assert entry["description"].strip()


def test_rule_types_is_reachable_by_any_authenticated_caller_with_no_space_at_all(
    api: Api, alice: User
) -> None:
    """No ``space_a`` fixture used here at all — proving this route needs no
    membership, and in fact no Space to exist, unlike everything under
    ``/spaces/{public_id}``.
    """
    response = api.as_user(alice).get("/rule-types")

    assert response.status_code == 200
    assert len(response.json()) == 8


# --- GET /spaces/{public_id}/rules --------------------------------------------


def test_a_fresh_space_is_seeded_with_no_rule_rows_at_all(
    api: Api, alice: User, space_a: Space
) -> None:
    """``create_space`` seeds no ``space_rules`` row of any type (task 10.5).

    It used to seed ``availability_hours`` and ``session_length`` so a fresh
    Space was immediately bookable; what makes one bookable now is the live
    ``DEFAULT_SHAPE`` calendar-shape row written in the same transaction
    (``.claude/rules/identity-and-access.md``). So "not enforced" is the
    absence of a row here for *every* rule type, with no exception a new
    venue starts life carrying.
    """
    response = api.as_user(alice).get(f"/spaces/{space_a.public_id}/rules")

    assert response.status_code == 200
    assert response.json() == []


# --- POST /spaces/{public_id}/rules -------------------------------------------


def test_post_creates_a_row_and_round_trips_its_fields(
    api: Api, alice: User, space_a: Space
) -> None:
    payload = {
        "rule_type": "max_duration",
        "params": {"max_duration_minutes": 90},
        "applies_to": {"weekdays": [0, 2, 4]},
        "enabled": False,
    }

    response = api.as_user(alice).post(f"/spaces/{space_a.public_id}/rules", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["rule_type"] == "max_duration"
    assert body["params"] == {"max_duration_minutes": 90}
    assert body["applies_to"] == {"weekdays": [0, 2, 4]}
    assert body["enabled"] is False
    assert isinstance(body["id"], int)
    assert body["created_at"]
    assert body["updated_at"]


def test_post_unknown_rule_type_is_422(api: Api, alice: User, space_a: Space) -> None:
    payload = {"rule_type": "not_a_real_rule_type", "params": {}}

    response = api.as_user(alice).post(f"/spaces/{space_a.public_id}/rules", json=payload)

    assert response.status_code == 422


def test_post_missing_required_param_is_422_naming_it(
    api: Api, alice: User, space_a: Space
) -> None:
    payload = {"rule_type": "booking_horizon", "params": {}}

    response = api.as_user(alice).post(f"/spaces/{space_a.public_id}/rules", json=payload)

    assert response.status_code == 422
    assert "days" in response.json()["detail"]


def test_post_integer_param_below_minimum_is_422(api: Api, alice: User, space_a: Space) -> None:
    payload = {"rule_type": "booking_horizon", "params": {"days": 0}}

    response = api.as_user(alice).post(f"/spaces/{space_a.public_id}/rules", json=payload)

    assert response.status_code == 422
    assert "days" in response.json()["detail"]


def test_post_unknown_param_is_422_naming_it(api: Api, alice: User, space_a: Space) -> None:
    payload = {"rule_type": "booking_horizon", "params": {"days": 30, "bogus": 1}}

    response = api.as_user(alice).post(f"/spaces/{space_a.public_id}/rules", json=payload)

    assert response.status_code == 422
    assert "bogus" in response.json()["detail"]


@pytest.mark.parametrize(
    "rule_type, params",
    [
        ("availability_hours", {"opens_at_minutes": 9 * 60, "closes_at_minutes": 17 * 60}),
        ("session_length", {"session_minutes": 30}),
    ],
)
def test_post_a_retired_rule_type_is_422(
    api: Api, alice: User, space_a: Space, rule_type: str, params: dict
) -> None:
    """``availability_hours`` and ``session_length`` were retired with the calendar shape.

    Retirement means **gone**, not deprecated (``.claude/rules/calendar-shape.md``): both left
    ``rules.REGISTRY``, so a payload naming either is refused by the same unknown-type path any
    invented id takes, with no lingering acceptance for the two ids an admin's stale bookmark or
    an old client might still send. This is the guard that would catch either one being quietly
    re-registered — leaving an admin two places to configure hours that can disagree, which is
    the state this stream exists to end.
    """
    response = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/rules", json={"rule_type": rule_type, "params": params}
    )

    assert response.status_code == 422


def test_post_on_an_archived_space_is_409(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    service.archive_space(session, space_a)

    response = api.as_user(alice).post(
        f"/spaces/{space_a.public_id}/rules",
        json={"rule_type": "booking_horizon", "params": {"days": 30}},
    )

    assert response.status_code == 409


# --- PATCH /spaces/{public_id}/rules/{rule_id} --------------------------------


def _create_rule(api: Api, alice: User, space: Space, **overrides: Any) -> dict:
    payload = {
        "rule_type": "max_duration",
        "params": {"max_duration_minutes": 60},
        "applies_to": None,
        "enabled": True,
    }
    payload.update(overrides)
    response = api.as_user(alice).post(f"/spaces/{space.public_id}/rules", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_patch_toggles_enabled(api: Api, alice: User, space_a: Space) -> None:
    rule = _create_rule(api, alice, space_a)

    response = api.as_user(alice).patch(
        f"/spaces/{space_a.public_id}/rules/{rule['id']}", json={"enabled": False}
    )

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    # Everything else untouched.
    assert response.json()["params"] == {"max_duration_minutes": 60}


def test_patch_sets_and_then_clears_applies_to(api: Api, alice: User, space_a: Space) -> None:
    rule = _create_rule(api, alice, space_a)
    url = f"/spaces/{space_a.public_id}/rules/{rule['id']}"

    set_response = api.as_user(alice).patch(url, json={"applies_to": {"dates": ["2026-12-25"]}})
    assert set_response.status_code == 200
    assert set_response.json()["applies_to"] == {"dates": ["2026-12-25"]}

    clear_response = api.as_user(alice).patch(url, json={"applies_to": None})
    assert clear_response.status_code == 200
    assert clear_response.json()["applies_to"] is None


def test_patch_replaces_params_and_revalidates(api: Api, alice: User, space_a: Space) -> None:
    rule = _create_rule(api, alice, space_a)
    url = f"/spaces/{space_a.public_id}/rules/{rule['id']}"

    ok = api.as_user(alice).patch(url, json={"params": {"max_duration_minutes": 30}})
    assert ok.status_code == 200
    assert ok.json()["params"] == {"max_duration_minutes": 30}

    bad = api.as_user(alice).patch(url, json={"params": {"max_duration_minutes": 0}})
    assert bad.status_code == 422
    assert "max_duration_minutes" in bad.json()["detail"]
    # The bad PATCH must not have taken effect.
    unchanged = api.as_user(alice).get(f"/spaces/{space_a.public_id}/rules")
    row = next(r for r in unchanged.json() if r["id"] == rule["id"])
    assert row["params"] == {"max_duration_minutes": 30}


def test_patch_omitting_params_entirely_keeps_the_stored_ones(
    api: Api, alice: User, space_a: Space
) -> None:
    """A PATCH is validated against the row's **effective** params, not the payload alone.

    A submission that names no ``params`` at all — toggling ``enabled``, narrowing
    ``applies_to`` — must not be refused as "missing required parameter" for a value the caller
    never meant to touch, and must leave that value exactly as stored. Every registered type
    happens to carry a single parameter today, so this is the whole surface the merge still has;
    the two multi-parameter types it also covered were retired with the calendar shape (task
    10.5), and the resolution itself is unchanged.
    """
    rule = _create_rule(api, alice, space_a, rule_type="booking_horizon", params={"days": 30})
    url = f"/spaces/{space_a.public_id}/rules/{rule['id']}"

    response = api.as_user(alice).patch(url, json={"enabled": False})

    assert response.status_code == 200
    assert response.json()["params"] == {"days": 30}
    assert response.json()["enabled"] is False


def test_patch_null_params_is_422(api: Api, alice: User, space_a: Space) -> None:
    rule = _create_rule(api, alice, space_a)

    response = api.as_user(alice).patch(
        f"/spaces/{space_a.public_id}/rules/{rule['id']}", json={"params": None}
    )

    assert response.status_code == 422


# --- DELETE /spaces/{public_id}/rules/{rule_id} -------------------------------


def test_delete_removes_the_row(api: Api, alice: User, space_a: Space) -> None:
    rule = _create_rule(api, alice, space_a)

    response = api.as_user(alice).delete(f"/spaces/{space_a.public_id}/rules/{rule['id']}")
    assert response.status_code == 204

    listing = api.as_user(alice).get(f"/spaces/{space_a.public_id}/rules")
    assert rule["id"] not in [row["id"] for row in listing.json()]


# --- 404-not-403 for a rule id in another Space -------------------------------


def test_patch_on_a_rule_id_in_another_space_is_404(
    api: Api, session: Session, alice: User, bob: User, space_a: Space, space_b: Space
) -> None:
    rule = _create_rule(api, alice, space_a)

    response = api.as_user(alice).patch(
        f"/spaces/{space_b.public_id}/rules/{rule['id']}", json={"enabled": False}
    )

    # Alice is not even a member of Space B, so this is 404 for two
    # independent reasons at once -- the point is that neither leaks.
    assert response.status_code == 404


def test_delete_on_a_rule_id_in_another_space_is_404(
    api: Api, session: Session, alice: User, bob: User, space_a: Space, space_b: Space
) -> None:
    _add_member(session, space_b, alice, MembershipRole.ADMIN)
    rule = _create_rule(api, alice, space_a)

    response = api.as_user(alice).delete(f"/spaces/{space_b.public_id}/rules/{rule['id']}")

    assert response.status_code == 404
    # And it must still be there, reachable from its real Space.
    listing = api.as_user(alice).get(f"/spaces/{space_a.public_id}/rules")
    assert rule["id"] in [row["id"] for row in listing.json()]
