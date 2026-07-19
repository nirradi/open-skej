"""Shared FastAPI dependencies.

The booking driver is resolved through a dependency rather than imported as a
module-level singleton so tests can swap in a throwaway driver via
``app.dependency_overrides[get_driver]``. Because the override replaces the
callable outright, an overridden test never constructs the real driver and so
never touches the on-disk ``./skej.db``.
"""

import os
from functools import lru_cache

from app.db import DEFAULT_DATABASE_URL, BookingDriver, SQLiteBookingDriver

DATABASE_URL_ENV = "SKEJ_DATABASE_URL"


@lru_cache(maxsize=1)
def get_driver() -> BookingDriver:
    """The process-wide booking driver, built on first use.

    Cached so every request shares one engine and connection pool. The URL is
    read from the environment so the Playwright suite (task 1.9) can point the
    backend at a throwaway database file without code changes.
    """
    return SQLiteBookingDriver(os.environ.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL))
