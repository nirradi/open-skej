"""Seed the former default booking target.

Task 4.4 through 4.10 booked the unscoped ``POST /bookings`` route against a
fixed default Resource and user (``app.db.constants``), so ``bookings.resource_id``
/ ``bookings.user_id`` — real foreign keys since 4.2 — needed a real row to point
at: a default user, a default Space to hold it, and a default Resource inside
that Space. This module plants those three rows.

Task 4.11 deleted that route, so nothing books against these rows any more. The
plant stays anyway: the backend ``driver`` test fixture and the sandbox seed both
still call it, and removing it would mean reworking their id-sequence handling
(``app.sandbox_seed._sync_sequence_past_explicit_defaults``) for no behavioural
gain — an inert row with a stable id is cheaper to keep than to unwind.

It is **deliberately not a migration.** A data seed in the schema history would
reach every database the migrations are ever run against, including a real one,
and plant a phantom Space and user there. Instead the seed lives here and is run
only against a disposable database — the backend ``driver`` test fixture and the
sandbox's setup — so nothing outside a throwaway database ever grows these rows.

The rows are inserted with an **explicit primary key of 1** so the ids in
``app.db.constants`` are stable without a lookup, and the insert is idempotent:
if the default user is already present the function does nothing, so re-running
the sandbox setup neither duplicates nor errors.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.constants import DEFAULT_RESOURCE_ID, DEFAULT_USER_ID
from app.identity.models import Resource, Space, User

# A public_id of the required shape (>= 20 chars) that names itself. The default
# Space is never handed out as a capability — it exists only to parent the default
# Resource — so its link being readable in the source is not a disclosure.
DEFAULT_SPACE_PUBLIC_ID = "default-booking-target"
DEFAULT_USER_AUTH0_SUB = "bootstrap|default-booking-target"
DEFAULT_USER_EMAIL = "default@localhost"


def ensure_booking_defaults(session: Session) -> None:
    """Idempotently plant the default user, Space and Resource.

    A no-op if the default user already exists. Otherwise all three rows are
    written in one transaction, each with an explicit id of 1, so a booking made
    by the unscoped routes has a valid ``user_id`` and ``resource_id`` to point at.
    """
    existing = session.execute(
        select(User.id).where(User.id == DEFAULT_USER_ID)
    ).scalar_one_or_none()
    if existing is not None:
        return

    session.add(
        User(
            id=DEFAULT_USER_ID,
            auth0_sub=DEFAULT_USER_AUTH0_SUB,
            email=DEFAULT_USER_EMAIL,
            name="Default",
        )
    )
    session.flush()
    session.add(
        Space(
            id=1,
            public_id=DEFAULT_SPACE_PUBLIC_ID,
            name="Default",
            created_by_user_id=DEFAULT_USER_ID,
            timezone="UTC",
        )
    )
    session.flush()
    session.add(Resource(id=DEFAULT_RESOURCE_ID, space_id=1, name="Default"))
    session.commit()


def main() -> None:
    """Run the seed against ``DATABASE_URL`` — the entry point the sandbox calls."""
    from app.db.session import get_session_factory

    with get_session_factory()() as session:
        ensure_booking_defaults(session)


if __name__ == "__main__":
    main()
