"""Tests for the booking endpoints.

The driver is overridden with the Postgres ``driver`` fixture from ``conftest.py``
(a freshly created ``bookings`` table per test). That is not only for isolation:
``get_driver`` builds the process-wide driver on the shared engine on first call,
so leaving it un-overridden would have the suite writing to the configured
database. The module is Postgres-only — it skips when ``DATABASE_URL`` is unset.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_driver
from app.main import app
from tests.conftest import requires_postgres

pytestmark = requires_postgres

# Tomorrow, not a fixed date. These tests drive the real endpoint, which calls
# ``evaluate`` without a Context and so judges the booking horizon against the
# wall clock. A hardcoded date would start failing as "that time has already
# passed" the day it went by.
DAY = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
    hour=0, minute=0, second=0, microsecond=0
)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


def iso(value: datetime) -> str:
    return value.isoformat()


@pytest.fixture
def client(driver):
    app.dependency_overrides[get_driver] = lambda: driver
    yield TestClient(app)
    app.dependency_overrides.clear()


def all_bookings(driver, *, include_cancelled: bool = False):
    """Everything the driver holds, for asserting on what was actually persisted."""
    return driver.list_bookings(
        start=DAY - timedelta(days=365),
        end=DAY + timedelta(days=365),
        include_cancelled=include_cancelled,
    )


def test_root_still_says_hello() -> None:
    """The scaffold route survives the router and CORS being added."""
    assert TestClient(app).get("/").json() == {"message": "Hello World"}


def test_create_booking_returns_the_persisted_booking(client, driver) -> None:
    response = client.post("/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(11))})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "confirmed"
    assert datetime.fromisoformat(body["start_at"]) == at(10)
    assert datetime.fromisoformat(body["end_at"]) == at(11)
    assert body["id"] is not None

    stored = all_bookings(driver)
    assert [b.id for b in stored] == [body["id"]]


def test_rule_denial_returns_422_and_persists_nothing(client, driver) -> None:
    """A 3-hour booking trips the stub's max-duration rule."""
    response = client.post("/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(13))})

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "rule_denied"
    assert "at most 2 hours" in body["message"]

    # The driver must never have been reached — including by a write that was
    # rolled back, which would still have burned an autoincrement id.
    assert all_bookings(driver, include_cancelled=True) == []


def test_rules_run_before_the_driver_is_touched(driver) -> None:
    """Pins the ordering directly, not just its side effect.

    ``test_rule_denial_returns_422_and_persists_nothing`` would also pass if the
    endpoint called the driver and then deleted the row. This one fails outright
    if the driver is reached at all.
    """

    class ExplodingDriver:
        def __getattr__(self, name):
            raise AssertionError(f"driver.{name} was called before the rules ran")

    app.dependency_overrides[get_driver] = ExplodingDriver
    try:
        response = TestClient(app).post(
            "/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(13))}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_denial_is_distinguishable_from_a_validation_error(client) -> None:
    """Both are 422; only the rule denial carries the ``error`` discriminator."""
    naive = client.post(
        "/bookings", json={"start_at": "2026-07-20T10:00:00", "end_at": "2026-07-20T11:00:00"}
    )

    assert naive.status_code == 422
    assert "error" not in naive.json()
    assert "detail" in naive.json()


def test_overlapping_booking_returns_409(client, driver) -> None:
    first = client.post("/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(11))})
    assert first.status_code == 201

    # Starts inside the existing booking and runs past its end.
    response = client.post("/bookings", json={"start_at": iso(at(10, 30)), "end_at": iso(at(12))})

    assert response.status_code == 409
    assert response.json()["error"] == "overlap"
    assert response.json()["message"]

    assert len(all_bookings(driver)) == 1


def test_adjacent_booking_is_not_a_conflict(client) -> None:
    """Half-open intervals: ending exactly when the next begins is fine."""
    client.post("/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(11))})

    response = client.post("/bookings", json={"start_at": iso(at(11)), "end_at": iso(at(12))})

    assert response.status_code == 201


def test_get_returns_only_bookings_in_the_window(client, driver) -> None:
    inside = driver.create_booking(start_at=at(10), end_at=at(11))
    straddling = driver.create_booking(start_at=at(8), end_at=at(9, 30))
    driver.create_booking(start_at=at(14), end_at=at(15))  # wholly after the window

    response = client.get("/bookings", params={"from": iso(at(9)), "to": iso(at(12))})

    assert response.status_code == 200
    returned = {b["id"] for b in response.json()}
    # The straddling booking overlaps the window, so it must be included: the
    # calendar has to draw it or its slot would render as free.
    assert returned == {inside.id, straddling.id}


def test_get_excludes_cancelled_bookings(client, driver) -> None:
    live = driver.create_booking(start_at=at(10), end_at=at(11))
    cancelled = driver.create_booking(start_at=at(12), end_at=at(13))
    driver.cancel_booking(cancelled.id)

    response = client.get("/bookings", params={"from": iso(at(0)), "to": iso(at(23))})

    assert [b["id"] for b in response.json()] == [live.id]


def test_get_includes_cancelled_when_asked(client, driver) -> None:
    live = driver.create_booking(start_at=at(10), end_at=at(11))
    cancelled = driver.create_booking(start_at=at(12), end_at=at(13))
    driver.cancel_booking(cancelled.id)

    response = client.get(
        "/bookings",
        params={"from": iso(at(0)), "to": iso(at(23)), "include_cancelled": "true"},
    )

    assert response.status_code == 200
    returned = {b["id"]: b["status"] for b in response.json()}
    assert returned == {live.id: "confirmed", cancelled.id: "cancelled"}


def test_cancel_returns_the_cancelled_booking(client, driver) -> None:
    """200 with the row, not 204: the client needs status and cancelled_at back."""
    created = client.post("/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(11))}).json()

    response = client.delete(f"/bookings/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["status"] == "cancelled"
    assert body["cancelled_at"] is not None

    stored = all_bookings(driver, include_cancelled=True)
    # Soft delete: the row survives, because Stream 3's rules count history.
    assert [b.id for b in stored] == [created["id"]]


def test_cancel_unknown_id_returns_404(client) -> None:
    response = client.delete("/bookings/4242")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert response.json()["message"]


def test_cancel_twice_returns_409(client) -> None:
    created = client.post("/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(11))}).json()
    assert client.delete(f"/bookings/{created['id']}").status_code == 200

    response = client.delete(f"/bookings/{created['id']}")

    assert response.status_code == 409
    # Same status as an overlap conflict, different discriminator — the whole
    # reason the client is told to branch on ``error`` and not on the code.
    assert response.json()["error"] == "already_cancelled"


def test_cancel_does_not_run_the_rule_engine(client) -> None:
    """The rule engine must not be consulted on the way out.

    Asserts the ordering directly rather than via a side effect: any call to
    ``evaluate`` from the cancel path raises, so wiring the engine in here fails
    the test outright instead of merely changing a status code.
    """
    created = client.post("/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(11))}).json()

    def exploding_evaluate(_request):
        raise AssertionError("the rule engine must not run on cancel")

    import app.routers.bookings as bookings_module

    original = bookings_module.evaluate
    bookings_module.evaluate = exploding_evaluate
    try:
        response = client.delete(f"/bookings/{created['id']}")
    finally:
        bookings_module.evaluate = original

    assert response.status_code == 200


def test_cancelled_slot_can_be_rebooked_over_http(client, driver) -> None:
    """The point of the soft delete: cancelling frees the interval end to end.

    Exercised entirely through HTTP rather than the driver, because that is the
    path task 1.8's cancel-then-rebook UI will take.
    """
    first = client.post("/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(11))}).json()

    # Same slot while the booking is live: refused.
    blocked = client.post("/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(11))})
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "overlap"

    assert client.delete(f"/bookings/{first['id']}").status_code == 200

    # Same slot again now that it is free: accepted.
    rebooked = client.post("/bookings", json={"start_at": iso(at(10)), "end_at": iso(at(11))})
    assert rebooked.status_code == 201
    assert rebooked.json()["id"] != first["id"]

    # The calendar sees only the new booking...
    live = client.get("/bookings", params={"from": iso(at(0)), "to": iso(at(23))}).json()
    assert [b["id"] for b in live] == [rebooked.json()["id"]]

    # ...while both rows are still on disk.
    assert len(all_bookings(driver, include_cancelled=True)) == 2


def test_get_rejects_an_inverted_window(client) -> None:
    response = client.get("/bookings", params={"from": iso(at(12)), "to": iso(at(9))})

    assert response.status_code == 400


def test_get_rejects_a_naive_window(client) -> None:
    response = client.get(
        "/bookings", params={"from": "2026-07-20T09:00:00", "to": "2026-07-20T12:00:00"}
    )

    assert response.status_code == 400


def test_cors_allows_the_vite_dev_origin(client) -> None:
    response = client.options(
        "/bookings",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
