"""Postgres migration tests for the unified Alembic setup.

The whole module is skipped when ``DATABASE_URL`` is unset so the rest of the
suite keeps running with no Postgres in sight. CI provides a ``postgres:16``
service and sets the variable, so these run there.

One migration history now owns the whole schema — the identity tables and the
``bookings`` table both — so there is no table-scoping filter to exercise. What
these prove instead is that the history is complete and honest: upgrading to head
creates both halves, and an autogenerate against head wants no table changes, so
``Base.metadata`` and the migrations agree.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.db.models import Base

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; Postgres migration tests need `docker compose up -d`",
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# Both halves of the schema. `bookings` was created outside migrations until
# Stream 4 folded it into the single history.
IDENTITY_TABLES = {"users", "spaces"}
BOOKING_TABLE = "bookings"


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


def test_upgrade_head_creates_both_halves(alembic_config, engine):
    """Upgrading to head brings the identity tables *and* `bookings` into being.

    The inverse of the retired filter test: `bookings` used to be deliberately
    absent from the migration history. Now the single history owns it, so a schema
    upgraded to head without a `bookings` table would mean the fold-in regressed.
    """
    from alembic import command

    command.upgrade(alembic_config, "head")
    try:
        tables = set(inspect(engine).get_table_names())
        assert IDENTITY_TABLES <= tables, f"identity tables missing: {IDENTITY_TABLES - tables}"
        assert BOOKING_TABLE in tables, "the bookings table was not created by any migration"
    finally:
        command.downgrade(alembic_config, "base")


def _table_change_ops(ops) -> set[str]:
    """Every table an autogenerate op tree would create or drop.

    Ops must be walked rather than repr'd: alembic's op classes use the default
    object repr, so a substring check against `repr(upgrade_ops)` can never fail
    and would pass even against a genuine diff. Only table-level create/drop is
    inspected — unambiguous, unlike a column-type nuance that varies by driver
    version.
    """
    from alembic.operations.ops import CreateTableOp, DropTableOp

    names: set[str] = set()
    for op in getattr(ops, "ops", []):
        if isinstance(op, (CreateTableOp, DropTableOp)):
            names.add(str(op.table_name))
        names |= _table_change_ops(op)
    return names


def test_autogenerate_after_head_wants_no_table_changes(alembic_config, engine):
    """Against a database at head, autogenerate must emit no create/drop table.

    This is the end-to-end proof that the migrations describe `Base.metadata` in
    full: every table the models declare — both halves — already exists, so a
    fresh comparison finds nothing to create, and no stray table to drop. A
    `bookings` model that had drifted from its migration would surface here.
    """
    from alembic import command
    from alembic.autogenerate import produce_migrations
    from alembic.migration import MigrationContext

    command.upgrade(alembic_config, "head")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True},
            )
            migrations = produce_migrations(context, Base.metadata)

        changed = _table_change_ops(migrations.upgrade_ops)
        assert not changed, f"autogenerate wants table changes at head: {changed}"
    finally:
        command.downgrade(alembic_config, "base")
