"""SQLite implementation of BookingDriver."""

import sqlite3
from datetime import datetime

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.constants import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID
from app.db.driver import (
    BookingAlreadyCancelledError,
    BookingNotFoundError,
    OverlapError,
)
from app.db.models import Base, Booking, BookingStatus, utcnow

DEFAULT_DATABASE_URL = "sqlite+pysqlite:///./skej.db"

# How long a writer waits for a competing writer's lock before giving up.
_BUSY_TIMEOUT_MS = 5000


def _configure_sqlite(engine: Engine) -> None:
    """Route every transaction on this engine through BEGIN IMMEDIATE.

    SQLite has no exclusion constraint, so create_booking's overlap check and its
    insert must not interleave with a competing writer. A default DEFERRED
    transaction takes no write lock until its first write, which would let two
    connections both read "no conflict" before either inserts. BEGIN IMMEDIATE
    takes the write lock up front, so the check-then-insert is genuinely
    serialised rather than best-effort.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        # pysqlite would otherwise open its own implicit transactions, which
        # cannot be upgraded to IMMEDIATE. None hands control back to us.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: object) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")  # type: ignore[attr-defined]


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


class SQLiteBookingDriver:
    """Local-development persistence backed by a single SQLite file."""

    def __init__(self, url: str = DEFAULT_DATABASE_URL, *, create_schema: bool = True) -> None:
        self._engine = create_engine(url)
        _configure_sqlite(self._engine)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)
        if create_schema:
            Base.metadata.create_all(self._engine)

    def list_bookings(
        self,
        *,
        start: datetime,
        end: datetime,
        resource_id: str = DEFAULT_RESOURCE_ID,
        include_cancelled: bool = False,
    ) -> list[Booking]:
        _require_aware("start", start)
        _require_aware("end", end)

        # Same half-open predicate as the overlap check: a booking is in the
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
        user_id: str = DEFAULT_USER_ID,
        resource_id: str = DEFAULT_RESOURCE_ID,
    ) -> Booking:
        _require_aware("start_at", start_at)
        _require_aware("end_at", end_at)
        if start_at >= end_at:
            raise ValueError("start_at must be strictly before end_at")

        with self._session_factory() as session:
            # session.begin() emits BEGIN IMMEDIATE via the event hook above, so
            # the write lock is held before the overlap probe runs.
            with session.begin():
                if self._find_overlap(session, resource_id, start_at, end_at) is not None:
                    raise OverlapError(
                        f"{resource_id} is already booked between "
                        f"{start_at.isoformat()} and {end_at.isoformat()}"
                    )
                booking = Booking(
                    resource_id=resource_id,
                    user_id=user_id,
                    start_at=start_at,
                    end_at=end_at,
                    status=BookingStatus.CONFIRMED,
                    created_at=utcnow(),
                    cancelled_at=None,
                )
                session.add(booking)
            return booking

    def cancel_booking(self, booking_id: int) -> Booking:
        with self._session_factory() as session:
            with session.begin():
                booking = session.get(Booking, booking_id)
                if booking is None:
                    raise BookingNotFoundError(f"no booking with id {booking_id}")
                if booking.status is BookingStatus.CANCELLED:
                    raise BookingAlreadyCancelledError(f"booking {booking_id} is already cancelled")
                booking.status = BookingStatus.CANCELLED
                booking.cancelled_at = utcnow()
            return booking

    @property
    def engine(self) -> Engine:
        return self._engine

    def close(self) -> None:
        self._engine.dispose()

    @staticmethod
    def _find_overlap(
        session: Session, resource_id: str, start_at: datetime, end_at: datetime
    ) -> int | None:
        """Return the id of a confirmed booking overlapping [start_at, end_at).

        The half-open predicate `existing.start_at < new.end_at AND
        new.start_at < existing.end_at` handles variable-length bookings without
        special-casing, and treats touching intervals (prev.end_at ==
        next.start_at) as non-overlapping. Only confirmed rows are considered, so
        cancelling a booking frees its interval for rebooking.
        """
        return session.execute(
            select(Booking.id)
            .where(
                Booking.resource_id == resource_id,
                Booking.status == BookingStatus.CONFIRMED,
                Booking.start_at < end_at,
                Booking.end_at > start_at,
            )
            .limit(1)
        ).scalar_one_or_none()
