"""Booking persistence: models, the driver contract, and the SQLite driver."""

from app.db.constants import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID
from app.db.driver import (
    BookingAlreadyCancelledError,
    BookingDriver,
    BookingError,
    BookingNotFoundError,
    OverlapError,
)
from app.db.models import Base, Booking, BookingStatus
from app.db.sqlite import DEFAULT_DATABASE_URL, SQLiteBookingDriver

__all__ = [
    "DEFAULT_DATABASE_URL",
    "DEFAULT_RESOURCE_ID",
    "DEFAULT_USER_ID",
    "Base",
    "Booking",
    "BookingAlreadyCancelledError",
    "BookingDriver",
    "BookingError",
    "BookingNotFoundError",
    "BookingStatus",
    "OverlapError",
    "SQLiteBookingDriver",
]
