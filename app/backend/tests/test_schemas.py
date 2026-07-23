"""Tests for the booking wire schemas."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.db import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID, BookingStatus
from app.schemas import BookingCreate, BookingRead
from tests.conftest import requires_postgres

START = datetime(2026, 7, 20, 10, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


def test_offset_aware_payload_parses():
    payload = BookingCreate.model_validate(
        {"start_at": START.isoformat(), "end_at": END.isoformat()}
    )

    assert payload.start_at == START
    assert payload.end_at == END


def test_naive_payload_is_rejected():
    with pytest.raises(ValidationError):
        BookingCreate.model_validate({"start_at": "2026-07-20T10:00:00", "end_at": END.isoformat()})


def test_non_positive_interval_is_rejected():
    with pytest.raises(ValidationError):
        BookingCreate.model_validate({"start_at": END.isoformat(), "end_at": START.isoformat()})


def test_to_rule_request_carries_identity_and_interval():
    payload = BookingCreate(start_at=START, end_at=END)

    rule_request = payload.to_rule_request(user_id=DEFAULT_USER_ID, resource_id=DEFAULT_RESOURCE_ID)

    # The engine identifies user and resource by opaque string label, so the
    # integer foreign-key ids are stringified at this boundary.
    assert rule_request.user_id == str(DEFAULT_USER_ID)
    assert rule_request.resource_id == str(DEFAULT_RESOURCE_ID)
    assert rule_request.start_at == START
    assert rule_request.end_at == END


@requires_postgres
def test_booking_read_serialises_a_stored_booking(driver):
    booking = driver.create_booking(start_at=START, end_at=END)

    read = BookingRead.model_validate(booking)

    assert read.id == booking.id
    assert read.start_at == START
    assert read.status is BookingStatus.CONFIRMED
    assert read.cancelled_at is None
