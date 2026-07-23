"""Postgres implementation of BookingDriver.

Where the SQLite driver serialised its own check-then-insert behind ``BEGIN
IMMEDIATE`` because SQLite has no exclusion constraint, Postgres enforces the
overlap invariant declaratively — ``ex_bookings_confirmed_no_overlap``, the
``EXCLUDE USING gist`` constraint created by the ``bookings`` migration. So
``create_booking`` here does not probe first: it inserts, and a conflicting
confirmed booking surfaces as an ``ExclusionViolation`` (SQLSTATE ``23P01``),
which is mapped to ``OverlapError``. The database is the single arbiter, so two
concurrent inserts cannot both win no matter how they interleave.

The driver is handed the process-wide session factory from ``app.db.session``
rather than building its own engine — one engine, one pool, shared with the
identity layer. Tests construct it with a factory bound to a throwaway database.
"""

from datetime import datetime

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.constants import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID
from app.db.driver import (
    BookingAlreadyCancelledError,
    BookingNotFoundError,
    OverlapError,
)
from app.db.models import Booking, BookingStatus, utcnow

# SQLSTATE for a Postgres exclusion_violation — what the overlap constraint
# raises. Matched by code rather than by parsing the message so a locale or a
# constraint rename cannot turn a genuine overlap into an uncaught 500.
_EXCLUSION_VIOLATION = "23P01"


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def _is_exclusion_violation(exc: IntegrityError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == _EXCLUSION_VIOLATION


class PostgresBookingDriver:
    """Production persistence backed by Postgres and the exclusion constraint."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_bookings(
        self,
        *,
        start: datetime,
        end: datetime,
        resource_id: int = DEFAULT_RESOURCE_ID,
        include_cancelled: bool = False,
    ) -> list[Booking]:
        _require_aware("start", start)
        _require_aware("end", end)

        # Half-open overlap predicate, same as the constraint: a booking is in the
        # window if it overlaps it at all, not only if it is fully contained.
        stmt = (
            select(Booking)
            .where(
                Booking.resource_id == resource_id,
                Booking.start_at < end,
                Booking.end_at > start,
            )
            .order_by(Booking.start_at, Booking.id)
        )
        if not include_cancelled:
            stmt = stmt.where(Booking.status == BookingStatus.CONFIRMED)

        with self._session_factory() as session:
            return list(session.execute(stmt).scalars())

    def create_booking(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        user_id: int = DEFAULT_USER_ID,
        resource_id: int = DEFAULT_RESOURCE_ID,
    ) -> Booking:
        _require_aware("start_at", start_at)
        _require_aware("end_at", end_at)
        if start_at >= end_at:
            raise ValueError("start_at must be strictly before end_at")

        booking = Booking(
            resource_id=resource_id,
            user_id=user_id,
            start_at=start_at,
            end_at=end_at,
            status=BookingStatus.CONFIRMED,
            created_at=utcnow(),
            cancelled_at=None,
        )
        with self._session_factory() as session:
            session.add(booking)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if _is_exclusion_violation(exc):
                    raise OverlapError(
                        f"{resource_id} is already booked between "
                        f"{start_at.isoformat()} and {end_at.isoformat()}"
                    ) from exc
                # A different integrity failure (a check constraint, a future FK)
                # is a bug or a caller error, not a slot clash — do not disguise it.
                raise
            return booking

    def cancel_booking(self, booking_id: int) -> Booking:
        with self._session_factory() as session:
            booking = session.get(Booking, booking_id)
            if booking is None:
                raise BookingNotFoundError(f"no booking with id {booking_id}")
            if booking.status is BookingStatus.CANCELLED:
                raise BookingAlreadyCancelledError(f"booking {booking_id} is already cancelled")
            booking.status = BookingStatus.CANCELLED
            booking.cancelled_at = utcnow()
            session.commit()
            return booking

    @property
    def engine(self) -> Engine:
        bind = self._session_factory.kw["bind"]
        assert isinstance(bind, Engine)
        return bind
