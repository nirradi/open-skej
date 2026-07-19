"""Postgres migration tests for Stream 2's Alembic setup.

The whole module is skipped when ``DATABASE_URL`` is unset so Stream 1's SQLite
suite keeps running standalone with no Postgres anywhere in sight. CI provides a
``postgres:16`` service and sets the variable, so these run there.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.db.models import Base
from app.migration_filter import STREAM_1_OWNED_TABLES, include_object

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; Postgres migration tests need `docker compose up -d`",
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def alembic_config():
    from alembic.config import Config

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    # script_location in the ini is %(here)s-relative, so it resolves regardless
    # of the directory pytest was invoked from.
    return config


@pytest.fixture
def engine():
    engine = create_engine(DATABASE_URL)
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_head_then_downgrade_base_runs_clean(alembic_config):
    """The full migration chain applies and unwinds without error."""
    from alembic import command

    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")


def test_migrations_never_create_stream_1_tables(alembic_config, engine):
    """Upgrading to head must not bring Stream 1's `bookings` table into being.

    Stream 1 creates that table itself via `Base.metadata.create_all` on SQLite.
    If a Stream 2 migration ever creates it on Postgres, the filter has leaked
    and the two streams are both claiming the same schema.
    """
    from alembic import command

    command.upgrade(alembic_config, "head")
    try:
        tables = set(inspect(engine).get_table_names())
        assert not (
            tables & STREAM_1_OWNED_TABLES
        ), f"Stream 2 migrations created Stream 1 tables: {tables & STREAM_1_OWNED_TABLES}"
    finally:
        command.downgrade(alembic_config, "base")


def _touched_table_names(ops) -> set[str]:
    """Every table name an autogenerate op tree refers to.

    Ops must be walked rather than repr'd: alembic's op classes use the default
    object repr, so a substring check against `repr(upgrade_ops)` can never fail
    and would pass even with the filter removed entirely.
    """
    names: set[str] = set()
    for op in getattr(ops, "ops", []):
        name = getattr(op, "table_name", None)
        if name is not None:
            names.add(str(name))
        names |= _touched_table_names(op)
    return names


def test_autogenerate_does_not_emit_stream_1_tables(engine):
    """Autogenerate against a real empty database must skip `bookings`.

    This is the end-to-end proof of the filter: `Base.metadata` genuinely
    contains the Booking model, and the database genuinely lacks the table, so an
    unfiltered comparison would certainly emit a `create_table('bookings')`.
    """
    from alembic.autogenerate import produce_migrations
    from alembic.migration import MigrationContext

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"include_object": include_object, "compare_type": True},
        )
        migrations = produce_migrations(context, Base.metadata)

    touched = _touched_table_names(migrations.upgrade_ops)
    assert not (
        touched & STREAM_1_OWNED_TABLES
    ), f"autogenerate leaked Stream 1 tables: {touched & STREAM_1_OWNED_TABLES}"


# --- The filter hook itself, called directly. -------------------------------


def test_include_object_excludes_bookings_table():
    assert (
        include_object(Base.metadata.tables["bookings"], "bookings", "table", False, None) is False
    )


def test_include_object_allows_an_identity_table():
    """A Stream 2 table is included.

    Paired with the test above so the hook is proven to discriminate rather than
    to reject everything — a filter that always returned False would also keep
    `bookings` out, while silently blocking every migration Stream 2 needs.
    """
    from sqlalchemy import Column, Integer, MetaData, String, Table

    metadata = MetaData()
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("auth0_sub", String(255), unique=True),
    )

    assert include_object(users, "users", "table", False, None) is True


def test_include_object_excludes_indexes_owned_by_bookings():
    """Indexes are offered separately and must be filtered by their owning table.

    Excluding only the table would emit an index against a table the migration
    never creates.
    """
    bookings = Base.metadata.tables["bookings"]
    (index,) = [ix for ix in bookings.indexes if ix.name == "ix_bookings_resource_status_start"]

    assert include_object(index, index.name, "index", False, None) is False


def test_include_object_excludes_constraints_and_columns_owned_by_bookings():
    bookings = Base.metadata.tables["bookings"]

    column = bookings.c.resource_id
    assert include_object(column, column.name, "column", False, None) is False

    constraint = next(c for c in bookings.constraints if c.name == "ck_bookings_positive_duration")
    assert include_object(constraint, constraint.name, "check_constraint", False, None) is False
