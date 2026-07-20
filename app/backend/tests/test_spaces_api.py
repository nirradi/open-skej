"""End-to-end tests for the Space endpoints, run against real Postgres.

Postgres-only, following ``tests/test_migrations.py``: the whole module skips
when ``DATABASE_URL`` is unset so Stream 1's SQLite suite keeps running
standalone. The last-owner tests need ``SELECT ... FOR UPDATE``, which SQLite
does not have, and the identity schema uses partial indexes that SQLite ignores.

``get_current_user`` is overridden rather than exercised — token verification is
already covered independently in ``tests/test_auth_jwt.py``, and minting real
RS256 tokens here would test that suite a second time while making it harder to
see which caller a test is about.

**The headline test is** :func:`test_a_member_of_one_space_gets_404_on_every_route_of_another`.
It walks the application's own route table rather than a hand-written list, so a
Space route added by task 2.6 or 2.7 is covered the moment it is registered —
which is the point, since the route most likely to leak across tenants is the one
nobody remembered to add to a list.
"""

import os
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.models import Base
from app.db.session import get_session
from app.identity import service
from app.identity.authz import SPACE_NOT_FOUND_DETAIL, role_at_least
from app.identity.models import MembershipRole, Space, SpaceMembership, User
from app.main import app

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; Space API tests need `docker compose up -d`",
)

# A public_id of the right shape that belongs to no Space. Used to prove that a
# Space which exists but is not yours is indistinguishable from one that does not
# exist at all.
NONEXISTENT_PUBLIC_ID = "aaaaaaaaaaaaaaaaaaaaaa"


# --- Route discovery, for the parametrised isolation test. ------------------


_HTTP_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


def _space_scoped_routes() -> list[tuple[str, str]]:
    """Every route under ``/spaces/{public_id}`` except ``/preview``.

    Derived from the application's own OpenAPI schema so this cannot drift out of
    date: a Space route added by a later task is swept up the moment it is
    registered. The schema rather than ``app.routes`` because FastAPI keeps
    included routers in lazy wrapper objects, so ``app.routes`` does not contain
    the flattened paths — walking it silently yields nothing, which is precisely
    the failure :func:`test_the_route_table_actually_yielded_routes_to_test`
    exists to catch.

    Preview is excluded because it is *designed* to be reachable by any
    link-holder; it gets its own test below asserting exactly that.
    """
    found: set[tuple[str, str]] = set()
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/spaces/{public_id}") or path.endswith("/preview"):
            continue
        for method in operations:
            if method.upper() in _HTTP_METHODS:
                found.add((method.upper(), path))
    return sorted(found)


SPACE_SCOPED_ROUTES = _space_scoped_routes()

# Enough keys to satisfy any of these routes' bodies. The authorization
# dependency runs before body validation, so a route whose body this does not
# match still returns 404 rather than 422 — but sending something plausible keeps
# a genuine failure readable instead of ambiguous.
GENERIC_BODY: dict[str, Any] = {"name": "Renamed", "role": "member"}


# --- Fixtures. --------------------------------------------------------------


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
    inspected an ORM object afterwards would otherwise trigger a refresh against
    a session FastAPI has moved on from.
    """
    with Session(engine, expire_on_commit=False) as session:
        yield session


class Api:
    """A ``TestClient`` with a swappable caller.

    ``api.as_user(alice).get(...)`` reads as the sentence the test is making,
    and makes it impossible to issue a request without having said who is making
    it — which in a suite about cross-tenant isolation is the detail that matters
    most.
    """

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
    return _make_user(session, "auth0|carol", "carol@example.com")


@pytest.fixture
def space_a(session: Session, alice: User) -> Space:
    """Alice's Space. She is its owner and its only member."""
    return service.create_space(session, alice, name="Court A", description="Alice's court")


@pytest.fixture
def space_b(session: Session, bob: User) -> Space:
    """Bob's Space. Alice has no relationship with it whatsoever."""
    return service.create_space(session, bob, name="Court B", description="Bob's court")


def _add_member(
    session: Session, space: Space, user: User, role: MembershipRole
) -> SpaceMembership:
    membership = SpaceMembership(space_id=space.id, user_id=user.id, role=role)
    session.add(membership)
    session.commit()
    return membership


def _role_of(session: Session, space: Space, user: User) -> MembershipRole | None:
    membership = session.execute(
        select(SpaceMembership)
        .where(
            SpaceMembership.space_id == space.id,
            SpaceMembership.user_id == user.id,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    return None if membership is None else membership.role


# --- Cross-tenant isolation: the headline. ----------------------------------


@pytest.mark.parametrize(
    "method,path",
    SPACE_SCOPED_ROUTES,
    ids=[f"{method} {path}" for method, path in SPACE_SCOPED_ROUTES],
)
def test_a_member_of_one_space_gets_404_on_every_route_of_another(
    api: Api, alice: User, bob: User, space_a: Space, space_b: Space, method: str, path: str
) -> None:
    """Alice, an owner in her own Space, is a stranger in Bob's — on every route.

    404 rather than 403 throughout. A 403 would confirm that Bob's ``public_id``
    names a real Space, turning the capability URL into an oracle: an attacker
    holding a candidate id could ask the API whether it is live. The status code
    and the body must both be the same as for an id that names nothing.

    ``{user_id}`` resolves to Bob, a genuine member of Space B, so a 404 here
    cannot be explained away as "that user does not exist".
    """
    url = path.replace("{public_id}", space_b.public_id).replace("{user_id}", str(bob.id))

    response = api.as_user(alice).request(method, url, json=GENERIC_BODY)

    assert response.status_code == 404, (
        f"{method} {path} leaked Space B's existence to a non-member"
        f" (got {response.status_code}, body {response.text})"
    )
    assert response.json()["detail"] == SPACE_NOT_FOUND_DETAIL


@pytest.mark.parametrize(
    "method,path",
    SPACE_SCOPED_ROUTES,
    ids=[f"{method} {path}" for method, path in SPACE_SCOPED_ROUTES],
)
def test_an_existing_foreign_space_is_indistinguishable_from_a_missing_one(
    api: Api, alice: User, bob: User, space_a: Space, space_b: Space, method: str, path: str
) -> None:
    """The two 404s must be byte-identical, not merely both 404.

    If the bodies differed — a different message, a different error key — the
    oracle the status code closes would reopen one level down.
    """
    real = path.replace("{public_id}", space_b.public_id).replace("{user_id}", str(bob.id))
    fake = path.replace("{public_id}", NONEXISTENT_PUBLIC_ID).replace("{user_id}", str(bob.id))

    client = api.as_user(alice)
    foreign = client.request(method, real, json=GENERIC_BODY)
    missing = client.request(method, fake, json=GENERIC_BODY)

    assert foreign.status_code == missing.status_code
    assert foreign.json() == missing.json()


def test_the_route_table_actually_yielded_routes_to_test() -> None:
    """Guards the parametrisation itself.

    If ``_space_scoped_routes`` ever returned nothing — a prefix renamed, the
    router not registered — every isolation test above would silently vanish
    from the run and the suite would still be green.
    """
    assert len(SPACE_SCOPED_ROUTES) >= 6, SPACE_SCOPED_ROUTES


def test_preview_is_reachable_by_any_link_holder(
    api: Api, alice: User, space_a: Space, space_b: Space
) -> None:
    """The one deliberate exception, and the reason it is excluded above.

    A cold link-holder must be able to see enough to decide whether to ask for
    access — and must see nothing more.
    """
    response = api.as_user(alice).get(f"/spaces/{space_b.public_id}/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Court B"
    assert body["description"] == "Bob's court"
    assert body["status"] == "none"
    assert "members" not in body and "member_count" not in body
    assert "bookings" not in body


def test_preview_reports_membership_for_a_member(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).get(f"/spaces/{space_a.public_id}/preview")

    assert response.status_code == 200
    assert response.json()["status"] == "member"


def test_preview_404s_on_a_public_id_that_names_nothing(api: Api, alice: User) -> None:
    response = api.as_user(alice).get(f"/spaces/{NONEXISTENT_PUBLIC_ID}/preview")

    assert response.status_code == 404


# --- 404 vs 403: the distinction itself. ------------------------------------


def test_a_non_member_gets_404_not_403_on_space_detail(
    api: Api, alice: User, space_b: Space
) -> None:
    """Stated separately from the parametrised sweep because it is the rule.

    The sweep proves it holds everywhere; this names it, so a failure reads as
    "the 404-not-403 rule broke" rather than as one row of a table.
    """
    response = api.as_user(alice).get(f"/spaces/{space_b.public_id}")

    assert response.status_code == 404
    assert response.status_code != 403


def test_a_member_gets_403_on_an_admin_only_route(
    api: Api, session: Session, alice: User, carol: User, space_a: Space
) -> None:
    """403 is correct *here* — Carol is inside the Space and knows it exists.

    The pair with the test above is what proves the codes discriminate. An
    implementation that returned 404 for everything would pass the isolation
    sweep while breaking this, and one that returned 403 for everything would
    pass this while breaking the sweep.
    """
    _add_member(session, space_a, carol, MembershipRole.MEMBER)

    response = api.as_user(carol).patch(
        f"/spaces/{space_a.public_id}", json={"name": "Carol's court now"}
    )

    assert response.status_code == 403


def test_an_admin_gets_403_on_the_owner_only_archive_route(
    api: Api, session: Session, alice: User, carol: User, space_a: Space
) -> None:
    """Archiving is owner-only, so admin is not enough — one rung is still a gap."""
    _add_member(session, space_a, carol, MembershipRole.ADMIN)

    response = api.as_user(carol).post(f"/spaces/{space_a.public_id}/archive")

    assert response.status_code == 403


def test_role_ordering_is_owner_over_admin_over_member() -> None:
    """The comparison must not be alphabetical or declaration-ordered.

    Under plain string comparison ``"admin" < "member"``, which would rank a
    member above an admin and hand every member admin authority.
    """
    assert role_at_least(MembershipRole.OWNER, MembershipRole.ADMIN)
    assert role_at_least(MembershipRole.OWNER, MembershipRole.MEMBER)
    assert role_at_least(MembershipRole.ADMIN, MembershipRole.MEMBER)
    assert role_at_least(MembershipRole.MEMBER, MembershipRole.MEMBER)

    assert not role_at_least(MembershipRole.MEMBER, MembershipRole.ADMIN)
    assert not role_at_least(MembershipRole.MEMBER, MembershipRole.OWNER)
    assert not role_at_least(MembershipRole.ADMIN, MembershipRole.OWNER)


# --- Creation and listing. --------------------------------------------------


def test_creating_a_space_makes_the_creator_its_owner(
    api: Api, session: Session, alice: User
) -> None:
    response = api.as_user(alice).post("/spaces", json={"name": "New Court"})

    assert response.status_code == 201
    body = response.json()
    assert body["my_role"] == "owner"
    assert body["public_id"]

    space = session.execute(select(Space).where(Space.public_id == body["public_id"])).scalar_one()
    assert _role_of(session, space, alice) is MembershipRole.OWNER


def test_list_spaces_returns_only_the_callers_own(
    api: Api, alice: User, bob: User, space_a: Space, space_b: Space
) -> None:
    """The absence of Space B is the assertion that matters.

    There is no listing of all Spaces anywhere in the API, so a Space the caller
    has no membership row for must be invisible here.
    """
    body = api.as_user(alice).get("/spaces").json()

    assert [space["public_id"] for space in body] == [space_a.public_id]
    assert space_b.public_id not in {space["public_id"] for space in body}

    bobs = api.as_user(bob).get("/spaces").json()
    assert [space["public_id"] for space in bobs] == [space_b.public_id]


def test_list_spaces_excludes_archived_unless_asked(api: Api, alice: User, space_a: Space) -> None:
    client = api.as_user(alice)
    client.post(f"/spaces/{space_a.public_id}/archive")

    assert client.get("/spaces").json() == []

    included = client.get("/spaces", params={"include_archived": "true"}).json()
    assert [space["public_id"] for space in included] == [space_a.public_id]
    assert included[0]["archived_at"] is not None


# --- The last-owner invariant. ----------------------------------------------


def test_the_last_owner_cannot_be_demoted(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    """409, and — just as importantly — the membership is left exactly as it was.

    A Space with no owner could never be archived or managed again: only owners
    may archive, there is no ownership transfer, and there is no global
    superuser to repair it.
    """
    response = api.as_user(alice).patch(
        f"/spaces/{space_a.public_id}/members/{alice.id}", json={"role": "member"}
    )

    assert response.status_code == 409
    assert _role_of(session, space_a, alice) is MembershipRole.OWNER


def test_the_last_owner_cannot_be_removed(
    api: Api, session: Session, alice: User, space_a: Space
) -> None:
    response = api.as_user(alice).delete(f"/spaces/{space_a.public_id}/members/{alice.id}")

    assert response.status_code == 409
    assert _role_of(session, space_a, alice) is MembershipRole.OWNER


def test_an_owner_can_be_demoted_when_another_owner_remains(
    api: Api, session: Session, alice: User, carol: User, space_a: Space
) -> None:
    """The positive case, without which both tests above would be vacuous.

    An implementation that refused *every* demotion would pass them and fail
    here.
    """
    _add_member(session, space_a, carol, MembershipRole.OWNER)

    response = api.as_user(alice).patch(
        f"/spaces/{space_a.public_id}/members/{alice.id}", json={"role": "member"}
    )

    assert response.status_code == 200
    assert response.json()["role"] == "member"
    assert _role_of(session, space_a, alice) is MembershipRole.MEMBER
    assert _role_of(session, space_a, carol) is MembershipRole.OWNER


def test_an_owner_can_be_removed_when_another_owner_remains(
    api: Api, session: Session, alice: User, carol: User, space_a: Space
) -> None:
    _add_member(session, space_a, carol, MembershipRole.OWNER)

    response = api.as_user(alice).delete(f"/spaces/{space_a.public_id}/members/{carol.id}")

    assert response.status_code == 204
    assert _role_of(session, space_a, carol) is None
    assert _role_of(session, space_a, alice) is MembershipRole.OWNER


def test_a_plain_member_can_be_removed(
    api: Api, session: Session, alice: User, carol: User, space_a: Space
) -> None:
    _add_member(session, space_a, carol, MembershipRole.MEMBER)

    response = api.as_user(alice).delete(f"/spaces/{space_a.public_id}/members/{carol.id}")

    assert response.status_code == 204
    assert _role_of(session, space_a, carol) is None


def test_changing_the_role_of_a_non_member_is_404(
    api: Api, alice: User, carol: User, space_a: Space
) -> None:
    response = api.as_user(alice).patch(
        f"/spaces/{space_a.public_id}/members/{carol.id}", json={"role": "admin"}
    )

    assert response.status_code == 404


# --- Members. ---------------------------------------------------------------


def test_members_are_listed_to_a_member(
    api: Api, session: Session, alice: User, carol: User, space_a: Space
) -> None:
    _add_member(session, space_a, carol, MembershipRole.MEMBER)

    body = api.as_user(carol).get(f"/spaces/{space_a.public_id}/members").json()

    assert {member["email"] for member in body} == {alice.email, carol.email}
    assert {member["role"] for member in body} == {"owner", "member"}


def test_an_admin_can_promote_a_member(
    api: Api, session: Session, alice: User, carol: User, space_a: Space
) -> None:
    _add_member(session, space_a, carol, MembershipRole.MEMBER)

    response = api.as_user(alice).patch(
        f"/spaces/{space_a.public_id}/members/{carol.id}", json={"role": "admin"}
    )

    assert response.status_code == 200
    assert _role_of(session, space_a, carol) is MembershipRole.ADMIN


# --- Editing and archiving. -------------------------------------------------


def test_an_admin_can_rename_a_space(
    api: Api, session: Session, alice: User, carol: User, space_a: Space
) -> None:
    _add_member(session, space_a, carol, MembershipRole.ADMIN)

    response = api.as_user(carol).patch(
        f"/spaces/{space_a.public_id}", json={"name": "Centre Court"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Centre Court"
    assert response.json()["description"] == "Alice's court", "an omitted field is untouched"


def test_an_explicit_null_description_clears_it(api: Api, alice: User, space_a: Space) -> None:
    """Omitted and explicitly-null must mean different things in a PATCH."""
    response = api.as_user(alice).patch(f"/spaces/{space_a.public_id}", json={"description": None})

    assert response.status_code == 200
    assert response.json()["description"] is None
    assert response.json()["name"] == "Court A"


def test_archiving_stamps_archived_at(api: Api, alice: User, space_a: Space) -> None:
    response = api.as_user(alice).post(f"/spaces/{space_a.public_id}/archive")

    assert response.status_code == 200
    assert response.json()["archived_at"] is not None


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PATCH", "/spaces/{public_id}", {"name": "Renamed"}),
        ("POST", "/spaces/{public_id}/archive", None),
        ("PATCH", "/spaces/{public_id}/members/{user_id}", {"role": "admin"}),
        ("DELETE", "/spaces/{public_id}/members/{user_id}", None),
    ],
    ids=["rename", "re-archive", "change-role", "remove-member"],
)
def test_an_archived_space_rejects_mutations_with_409(
    api: Api,
    session: Session,
    alice: User,
    carol: User,
    space_a: Space,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    _add_member(session, space_a, carol, MembershipRole.MEMBER)
    client = api.as_user(alice)
    assert client.post(f"/spaces/{space_a.public_id}/archive").status_code == 200

    url = path.replace("{public_id}", space_a.public_id).replace("{user_id}", str(carol.id))
    response = client.request(method, url, json=body)

    assert response.status_code == 409, f"{method} {path} should be refused on an archived Space"


def test_an_archived_space_can_still_be_read(
    api: Api, session: Session, alice: User, carol: User, space_a: Space
) -> None:
    """Archiving is not deletion. A record you cannot read is not a record."""
    _add_member(session, space_a, carol, MembershipRole.MEMBER)
    client = api.as_user(alice)
    client.post(f"/spaces/{space_a.public_id}/archive")

    assert client.get(f"/spaces/{space_a.public_id}").status_code == 200
    assert client.get(f"/spaces/{space_a.public_id}/members").status_code == 200
    assert client.get(f"/spaces/{space_a.public_id}/preview").status_code == 200


# --- The integer id must never cross the wire. ------------------------------


def _assert_no_space_id(payload: Any, trail: str = "response") -> None:
    """Walk a decoded JSON body asserting no key discloses a Space's integer id.

    A key-name check rather than a value check: matching on the value alone would
    pass by luck whenever a Space's id happened to differ from every integer in
    the body, which for small test databases is most of the time. ``user_id`` is
    permitted — that is a deliberate disclosure, and the membership routes are
    addressed by it.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key not in {"id", "space_id"}, f"{trail} exposes Space.id via '{key}'"
            _assert_no_space_id(value, f"{trail}.{key}")
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            _assert_no_space_id(item, f"{trail}[{index}]")


def test_no_response_body_exposes_the_integer_space_id(
    api: Api, session: Session, alice: User, carol: User, space_a: Space
) -> None:
    """``public_id`` is the capability; the sequential integer is enumerable.

    Exposing the integer anywhere would let a caller reason about Spaces they
    were never handed a link to — the exact inference the random ``public_id``
    exists to prevent.
    """
    _add_member(session, space_a, carol, MembershipRole.MEMBER)
    client = api.as_user(alice)

    responses = [
        client.post("/spaces", json={"name": "Another"}),
        client.get("/spaces"),
        client.get(f"/spaces/{space_a.public_id}"),
        client.get(f"/spaces/{space_a.public_id}/preview"),
        client.get(f"/spaces/{space_a.public_id}/members"),
        client.patch(f"/spaces/{space_a.public_id}", json={"name": "Renamed"}),
        client.patch(f"/spaces/{space_a.public_id}/members/{carol.id}", json={"role": "admin"}),
        client.post(f"/spaces/{space_a.public_id}/archive"),
    ]

    for response in responses:
        assert response.status_code < 400, response.text
        _assert_no_space_id(response.json(), f"{response.request.method} {response.request.url}")


# --- Owner authority: admin+ manages members, but not owners. ----------------
#
# Managing members is delegable to admins; crossing into ownership is not. An
# admin who could grant the owner role could grant it to themselves, and from
# there archive the Space and demote the person who created it. The last-owner
# check does not contain that on its own — it only refuses to remove the *final*
# owner, so an admin could still evict or demote an owner whenever a second one
# existed.


def test_an_admin_cannot_promote_themselves_to_owner(
    api: Api, session: Session, alice: User, bob: User, space_a: Space
) -> None:
    """The escalation this rule exists to stop.

    Alice owns the Space; Bob is an admin. If Bob can PATCH his own membership to
    ``owner``, "admin" is not a delegation of authority but a path to seizing it.
    """
    _add_member(session, space_a, bob, MembershipRole.ADMIN)

    response = api.as_user(bob).patch(
        f"/spaces/{space_a.public_id}/members/{bob.id}", json={"role": "owner"}
    )

    assert response.status_code == 403, response.text
    assert _role_of(session, space_a, bob) is MembershipRole.ADMIN, "Bob must still be an admin"


def test_an_admin_cannot_demote_an_owner(
    api: Api, session: Session, alice: User, bob: User, carol: User, space_a: Space
) -> None:
    """Two owners exist, so the last-owner check would permit this. Authority does not."""
    _add_member(session, space_a, bob, MembershipRole.ADMIN)
    _add_member(session, space_a, carol, MembershipRole.OWNER)

    response = api.as_user(bob).patch(
        f"/spaces/{space_a.public_id}/members/{alice.id}", json={"role": "member"}
    )

    assert response.status_code == 403, response.text
    assert _role_of(session, space_a, alice) is MembershipRole.OWNER


def test_an_admin_cannot_remove_an_owner(
    api: Api, session: Session, alice: User, bob: User, carol: User, space_a: Space
) -> None:
    _add_member(session, space_a, bob, MembershipRole.ADMIN)
    _add_member(session, space_a, carol, MembershipRole.OWNER)

    response = api.as_user(bob).delete(f"/spaces/{space_a.public_id}/members/{alice.id}")

    assert response.status_code == 403, response.text
    assert _role_of(session, space_a, alice) is MembershipRole.OWNER


def test_an_admin_may_still_manage_ordinary_members(
    api: Api, session: Session, alice: User, bob: User, carol: User, space_a: Space
) -> None:
    """The positive control.

    Without this, the three tests above would all pass against a rule that simply
    forbade admins from touching memberships at all — which would break the
    delegation the admin role exists to provide.
    """
    _add_member(session, space_a, bob, MembershipRole.ADMIN)
    _add_member(session, space_a, carol, MembershipRole.MEMBER)

    promote = api.as_user(bob).patch(
        f"/spaces/{space_a.public_id}/members/{carol.id}", json={"role": "admin"}
    )
    assert promote.status_code == 200, promote.text
    assert _role_of(session, space_a, carol) is MembershipRole.ADMIN

    remove = api.as_user(bob).delete(f"/spaces/{space_a.public_id}/members/{carol.id}")
    assert remove.status_code == 204, remove.text
    assert _role_of(session, space_a, carol) is None


def test_an_owner_may_grant_ownership(
    api: Api, session: Session, alice: User, bob: User, space_a: Space
) -> None:
    """The other positive control: the rule gates on authority, not on the role name."""
    _add_member(session, space_a, bob, MembershipRole.MEMBER)

    response = api.as_user(alice).patch(
        f"/spaces/{space_a.public_id}/members/{bob.id}", json={"role": "owner"}
    )

    assert response.status_code == 200, response.text
    assert _role_of(session, space_a, bob) is MembershipRole.OWNER
