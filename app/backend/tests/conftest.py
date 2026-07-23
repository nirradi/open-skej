"""Shared Postgres fixtures for the backend test suite.

The booking driver is Postgres-only now (Stream 4 retired the SQLite driver), and
its overlap invariant lives in an ``EXCLUDE USING gist`` constraint that only
Postgres can enforce — so these tests need a real database. They skip when
``DATABASE_URL`` is unset, the same way the migration tests do, leaving the rest
of the suite runnable with no Postgres in sight.

``btree_gist`` is a property of the test database, not of any one fixture. The
overlap constraint's ``resource_id WITH =`` term needs that opclass, and because
one ``Base`` now spans the whole schema, *every* identity fixture that does
``Base.metadata.create_all`` builds the ``bookings`` table too — so the extension
is created once, autouse, for the session rather than in each fixture.

Each ``driver`` starts against a freshly created ``bookings`` table and drops it
afterwards, so tests are isolated from one another and from the migration tests
that share the database. The table is built with ``create_all`` rather than by
running Alembic — the migration itself is exercised in ``test_migrations`` — but
it is the same model, so the exclusion constraint is present either way.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db.bootstrap import ensure_booking_defaults
from app.db.models import Base
from app.db.postgres import PostgresBookingDriver
from app.identity.models import Resource

DATABASE_URL = os.environ.get("DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; the booking driver is Postgres-only",
)

# A second Resource, distinct from the seeded default (id 1), for the test that
# asserts the overlap invariant is scoped per Resource. Its own top-level constant
# so test_db can name it rather than reaching for a bare literal.
OTHER_RESOURCE_ID = 2


@pytest.fixture(scope="session", autouse=True)
def _btree_gist_extension():
    """Ensure ``btree_gist`` exists before anything builds the schema.

    Autouse and session-scoped so it runs ahead of every fixture that calls
    ``create_all`` — the driver fixtures here and the identity fixtures in the
    other modules alike. A no-op when Postgres is not configured; those modules
    skip anyway.
    """
    if not DATABASE_URL:
        yield
        return
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        yield
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def pg_engine():
    engine = create_engine(DATABASE_URL)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def driver(pg_engine):
    """A driver over a freshly built schema seeded with the default booking target.

    The whole schema rather than the ``bookings`` table alone: ``resource_id`` and
    ``user_id`` are now foreign keys onto ``resources`` and ``users``, so those
    tables must exist and hold the rows a booking points at. ``ensure_booking_defaults``
    plants the default Resource (id 1) and user (id 1) the unscoped driver books
    against; a second Resource is added so the resource-scoped overlap test has a
    distinct id to use.
    """
    Base.metadata.drop_all(pg_engine)
    Base.metadata.create_all(pg_engine)
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
    with factory() as session:
        ensure_booking_defaults(session)
        session.add(Resource(id=OTHER_RESOURCE_ID, space_id=1, name="Court 2"))
        session.commit()
    try:
        yield PostgresBookingDriver(factory)
    finally:
        Base.metadata.drop_all(pg_engine)
