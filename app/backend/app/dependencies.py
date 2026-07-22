"""Shared FastAPI dependencies.

The booking driver is resolved through a dependency rather than imported as a
module-level singleton so tests can swap in a throwaway driver via
``app.dependency_overrides[get_driver]``. Because the override replaces the
callable outright, an overridden test never constructs the real driver and so
never touches the configured database.

There is one database now: ``DATABASE_URL`` (Postgres). The booking driver shares
the process-wide engine and session factory in ``app.db.session`` with the
identity layer — one engine, one pool — so storage is unified behind a single
connection string rather than split across the retired ``SKEJ_DATABASE_URL``.
"""

from functools import lru_cache

from app.db import BookingDriver, PostgresBookingDriver
from app.db.session import get_session_factory


@lru_cache(maxsize=1)
def get_driver() -> BookingDriver:
    """The process-wide booking driver, built on first use.

    Cached so every request shares one driver over the shared engine. The engine
    itself is built lazily from ``DATABASE_URL`` the first time a session is
    opened, so importing this module without a database configured stays harmless.
    """
    return PostgresBookingDriver(get_session_factory())
