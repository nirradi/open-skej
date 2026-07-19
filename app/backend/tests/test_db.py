"""Tests for the booking data layer, focused on the overlap invariant."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.db import (
    DEFAULT_RESOURCE_ID,
    DEFAULT_USER_ID,
    BookingAlreadyCancelledError,
    BookingNotFoundError,
    BookingStatus,
    OverlapError,
    SQLiteBookingDriver,
)

DAY = datetime(2026, 7, 20, tzinfo=timezone.utc)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


@pytest.fixture
def driver(tmp_path):
    driver = SQLiteBookingDriver(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    yield driver
    driver.close()


@pytest.fixture
def booked(driver):
    """An existing confirmed booking from 10:00 to 11:00."""
    return driver.create_booking(start_at=at(10), end_at=at(11))


def test_create_booking_persists_a_confirmed_booking(driver):
    booking = driver.create_booking(start_at=at(10), end_at=at(11, 30))

    assert booking.id is not None
    assert booking.status is BookingStatus.CONFIRMED
    assert booking.cancelled_at is None
    assert booking.user_id == DEFAULT_USER_ID
    assert booking.resource_id == DEFAULT_RESOURCE_ID

    (stored,) = driver.list_bookings(start=at(0), end=at(24))
    assert stored.id == booking.id
    assert stored.start_at == at(10)
    assert stored.end_at == at(11, 30)
    assert stored.start_at.tzinfo is not None
    assert stored.created_at.tzinfo is not None


def test_bookings_are_variable_length(driver):
    short = driver.create_booking(start_at=at(9), end_at=at(9, 10))
    long = driver.create_booking(start_at=at(12), end_at=at(17, 45))

    assert long.end_at - long.start_at == timedelta(hours=5, minutes=45)
    assert short.end_at - short.start_at == timedelta(minutes=10)


def test_non_utc_input_is_stored_as_the_same_instant(driver):
    plus_three = timezone(timedelta(hours=3))
    booking = driver.create_booking(
        start_at=datetime(2026, 7, 20, 13, 0, tzinfo=plus_three),
        end_at=datetime(2026, 7, 20, 14, 0, tzinfo=plus_three),
    )

    (stored,) = driver.list_bookings(start=at(0), end=at(24))
    assert stored.id == booking.id
    assert stored.start_at == at(10)
    assert stored.start_at.utcoffset() == timedelta(0)


# --- Overlap edges -------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "start", "end"),
    [
        ("exact match", at(10), at(11)),
        ("partial overlap at front", at(9, 30), at(10, 30)),
        ("partial overlap at back", at(10, 30), at(11, 30)),
        ("new fully contained in existing", at(10, 15), at(10, 45)),
        ("existing fully contained in new", at(9), at(12)),
        ("shares only the start instant", at(10), at(10, 15)),
        ("shares only the end instant", at(10, 45), at(11)),
    ],
)
def test_overlapping_booking_is_rejected(driver, booked, label, start, end):
    with pytest.raises(OverlapError):
        driver.create_booking(start_at=start, end_at=end)

    assert len(driver.list_bookings(start=at(0), end=at(24))) == 1, label


@pytest.mark.parametrize(
    ("label", "start", "end"),
    [
        ("ends exactly when the existing one starts", at(9), at(10)),
        ("starts exactly when the existing one ends", at(11), at(12)),
    ],
)
def test_adjacent_booking_is_allowed(driver, booked, label, start, end):
    booking = driver.create_booking(start_at=start, end_at=end)

    assert booking.id is not None, label
    assert len(driver.list_bookings(start=at(0), end=at(24))) == 2


def test_overlap_is_scoped_to_the_resource(driver, booked):
    other = driver.create_booking(start_at=at(10), end_at=at(11), resource_id="court-2")

    assert other.id != booked.id
    assert driver.list_bookings(start=at(0), end=at(24), resource_id="court-2") == [other]


# --- Cancellation --------------------------------------------------------


def test_cancelling_frees_the_slot_for_rebooking(driver, booked):
    cancelled = driver.cancel_booking(booked.id)
    assert cancelled.status is BookingStatus.CANCELLED
    assert cancelled.cancelled_at is not None

    # The whole point: the overlap check ignores cancelled rows.
    rebooked = driver.create_booking(start_at=at(10), end_at=at(11))
    assert rebooked.id != booked.id

    assert driver.list_bookings(start=at(0), end=at(24)) == [rebooked]


def test_cancelled_bookings_are_hidden_unless_requested(driver, booked):
    driver.cancel_booking(booked.id)

    assert driver.list_bookings(start=at(0), end=at(24)) == []
    assert driver.list_bookings(start=at(0), end=at(24), include_cancelled=True) == [booked]


def test_cancelling_an_unknown_booking_raises(driver):
    with pytest.raises(BookingNotFoundError):
        driver.cancel_booking(4242)


def test_cancelling_twice_raises(driver, booked):
    driver.cancel_booking(booked.id)

    with pytest.raises(BookingAlreadyCancelledError):
        driver.cancel_booking(booked.id)


# --- Listing -------------------------------------------------------------


def test_list_bookings_returns_the_window_in_start_order(driver):
    afternoon = driver.create_booking(start_at=at(14), end_at=at(15))
    morning = driver.create_booking(start_at=at(9), end_at=at(10))
    driver.create_booking(start_at=at(20), end_at=at(21))

    assert driver.list_bookings(start=at(8), end=at(16)) == [morning, afternoon]


def test_list_bookings_includes_bookings_straddling_the_window_edges(driver):
    straddles_start = driver.create_booking(start_at=at(9), end_at=at(11))
    straddles_end = driver.create_booking(start_at=at(15), end_at=at(17))

    assert driver.list_bookings(start=at(10), end=at(16)) == [straddles_start, straddles_end]


def test_list_bookings_excludes_bookings_merely_touching_the_window(driver):
    driver.create_booking(start_at=at(8), end_at=at(10))
    driver.create_booking(start_at=at(16), end_at=at(18))

    assert driver.list_bookings(start=at(10), end=at(16)) == []


# --- Input validation ----------------------------------------------------


def test_naive_datetimes_are_rejected(driver):
    with pytest.raises(ValueError):
        driver.create_booking(start_at=datetime(2026, 7, 20, 10), end_at=at(11))


@pytest.mark.parametrize(
    ("start", "end"),
    [(at(11), at(10)), (at(10), at(10))],
    ids=["end before start", "zero length"],
)
def test_non_positive_intervals_are_rejected(driver, start, end):
    with pytest.raises(ValueError):
        driver.create_booking(start_at=start, end_at=end)


# --- Concurrency ---------------------------------------------------------


def _stall_after_overlap_probe(driver, barrier):
    """Hold each writer between its overlap probe and its insert.

    Simply starting two threads at once does not test anything: the winner
    reliably finishes before the loser issues its first statement, so the race
    never happens and the test passes even with the locking removed. Parking
    each connection *after* its overlap SELECT forces the interleaving that a
    DEFERRED transaction would get wrong — both readers seeing "no conflict"
    before either writes.

    Under BEGIN IMMEDIATE the second writer cannot reach the probe at all until
    the first commits, so the first waits out `barrier` and breaks it; the
    resulting BrokenBarrierError is the expected path, not a failure.
    """

    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT bookings.id") and "LIMIT" in statement:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass

    event.listen(driver.engine, "after_cursor_execute", after_cursor_execute)
    return after_cursor_execute


def test_racing_overlapping_creates_yield_exactly_one_success(driver):
    """Two overlapping writers must not both win."""
    barrier = threading.Barrier(2, timeout=0.5)
    listener = _stall_after_overlap_probe(driver, barrier)

    def attempt(offset_minutes: int):
        try:
            return driver.create_booking(
                start_at=at(10, offset_minutes), end_at=at(11, offset_minutes)
            )
        except OverlapError as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [0, 30]))
    finally:
        event.remove(driver.engine, "after_cursor_execute", listener)

    successes = [r for r in results if not isinstance(r, Exception)]
    overlaps = [r for r in results if isinstance(r, OverlapError)]

    # Anything else — two successes, or an OperationalError from a writer that
    # deadlocked upgrading a read transaction — means the slot was not serialised.
    assert len(successes) == 1, results
    assert len(overlaps) == 1, results
    assert driver.list_bookings(start=at(0), end=at(24)) == successes
