"""The storage-agnostic booking driver contract.

Stream 1 ships the SQLite implementation. Stream 2 provisions Postgres and can
supply its own implementation of this protocol — notably replacing the
transactional overlap check with a declarative exclusion constraint:

    EXCLUDE USING gist (resource_id WITH =, tstzrange(start_at, end_at) WITH &&)
"""

from datetime import datetime
from typing import Protocol

from app.db.constants import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID
from app.db.models import Booking


class BookingError(Exception):
    """Base class for every error the driver raises deliberately."""


class OverlapError(BookingError):
    """A confirmed booking already covers part of the requested interval.

    Distinct from a rule-engine denial: double-booking a shared resource is an
    integrity invariant of the data layer, not a configurable business rule.
    """


class BookingNotFoundError(BookingError):
    """No booking exists with the given id."""


class BookingAlreadyCancelledError(BookingError):
    """The booking exists but has already been cancelled."""


class BookingDriver(Protocol):
    def list_bookings(
        self,
        *,
        start: datetime,
        end: datetime,
        resource_id: str = DEFAULT_RESOURCE_ID,
        include_cancelled: bool = False,
    ) -> list[Booking]:
        """Return bookings overlapping the half-open window [start, end).

        Ordered by start time. Cancelled bookings are excluded unless asked for.
        """
        ...

    def create_booking(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        user_id: str = DEFAULT_USER_ID,
        resource_id: str = DEFAULT_RESOURCE_ID,
    ) -> Booking:
        """Persist a confirmed booking over [start_at, end_at).

        Raises OverlapError if a confirmed booking on the same resource overlaps,
        and ValueError if the interval is naive or non-positive.
        """
        ...

    def cancel_booking(self, booking_id: int) -> Booking:
        """Soft-cancel a booking, freeing its interval for rebooking.

        Raises BookingNotFoundError or BookingAlreadyCancelledError.
        """
        ...
