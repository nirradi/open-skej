"""SQLAlchemy models for the booking data layer."""

import enum
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, Enum, Index, String, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator[datetime]):
    """A DateTime that is always timezone-aware UTC on the Python side.

    SQLite has no timezone-aware storage type, so aware values are normalised to
    UTC and stored naive, then re-tagged as UTC on the way back out. Naive input
    is rejected rather than assumed to be UTC, so an accidental local time cannot
    silently land in the database and shift a booking by the offset.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect: Dialect) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected; pass a timezone-aware value")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(
        self, value: Optional[datetime], dialect: Dialect
    ) -> Optional[datetime]:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class BookingStatus(str, enum.Enum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


# native_enum=False stores the plain string values, which keeps the column
# readable in SQLite and portable to Postgres in Stream 2. create_constraint is
# off by default in SQLAlchemy 2.0, so it is set explicitly to get the
# CHECK (status IN ('confirmed', 'cancelled')) that backs the documented domain.
_STATUS_TYPE = Enum(
    BookingStatus,
    name="booking_status",
    native_enum=False,
    create_constraint=True,
    length=16,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)


class Base(DeclarativeBase):
    pass


class Booking(Base):
    """A reservation of a resource over a half-open interval [start_at, end_at).

    Bookings are variable length; nothing here assumes a fixed slot duration.
    Cancellation is a soft delete via ``status`` because Stream 3's rules count
    booking history, so a cancelled row must stay queryable.
    """

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    start_at: Mapped[datetime] = mapped_column(UtcDateTime)
    end_at: Mapped[datetime] = mapped_column(UtcDateTime)
    status: Mapped[BookingStatus] = mapped_column(_STATUS_TYPE, default=BookingStatus.CONFIRMED)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        CheckConstraint("start_at < end_at", name="ck_bookings_positive_duration"),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL)"
            " OR (status = 'confirmed' AND cancelled_at IS NULL)",
            name="ck_bookings_cancelled_at_matches_status",
        ),
        # Covers the overlap probe in create_booking and the window scan in
        # list_bookings, both of which filter on resource then status then time.
        Index("ix_bookings_resource_status_start", "resource_id", "status", "start_at"),
    )

    def __repr__(self) -> str:
        return (
            f"Booking(id={self.id!r}, resource_id={self.resource_id!r},"
            f" start_at={self.start_at!r}, end_at={self.end_at!r},"
            f" status={self.status.value!r})"
        )

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Booking):
            return NotImplemented
        return self.id is not None and self.id == other.id

    def __hash__(self) -> int:
        return hash((Booking, self.id))
