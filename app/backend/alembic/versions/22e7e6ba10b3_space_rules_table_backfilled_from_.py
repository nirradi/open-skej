"""space rules table, backfilled from spaces columns

The expand half of an expand-then-contract migration (``ops/plans/stream-6-
plan.md``, task 6.6; task 6.10 is the contract half). Rule configuration is
moving from seven fixed columns on ``spaces`` -- one value each, for the whole
venue -- into rows on the new ``space_rules`` table: one row per *instance* of
a rule type declared in ``rules.registry.REGISTRY``, so a Space can hold any
number of instances of a type and scope each with ``applies_to`` (see
``app.identity.models.SpaceRule`` for the column-by-column reasoning and the
``applies_to`` JSON shape).

**Backfill.** For every existing Space, one row is inserted per non-null
group of the old columns, mirroring ``app.rules_stub._build_canon``'s own
gating exactly:

* ``availability_hours`` -- only when *both* ``opens_at`` and ``closes_at``
  are set, never just one, the same "both or neither" the adapter has always
  enforced. ``params`` holds each bound as an ``isoformat()`` string.
* ``slot_alignment`` -- when ``slot_minutes`` is set.
* ``max_duration`` -- when ``max_duration_minutes`` is set.
* ``booking_horizon`` -- when ``booking_horizon_days`` is set. **The stored
  param is named ``days``**, not ``booking_horizon_days`` -- it must match
  ``rules.registry.REGISTRY["booking_horizon"]``'s declared parameter name,
  which does not always match the column it is sourced from.
* ``max_bookings_per_week`` / ``max_bookings_per_month`` -- when the
  corresponding column is set. **The stored param is named ``max_bookings``**
  for both, again matching the registry rather than the column name.

Every backfilled row gets ``applies_to = NULL`` (always) and ``enabled =
true``. **No ``not_in_the_past`` row is ever created** -- that rule takes no
parameters and is unconditional (``rules_stub._build_canon`` prepends
``NotInThePastRule()`` regardless of configuration, and 6.4's registry marks
it ``is_single`` for the same reason), so it has nothing to backfill and
stays synthesized by the adapter rather than stored.

**The downgrade rebuilds the seven columns from the rows** -- reading back
each ``rule_type``'s ``params`` and writing the matching column -- because
``test_migrations.py`` drives a full upgrade-then-downgrade-to-base cycle,
and a downgrade that cannot reconstruct the old shape fails that test
outright. Where a Space holds more than one row of a type that only had room
for one column (a state this migration itself can never produce, but the
schema does not prevent one being added between the upgrade and a later
downgrade), the *first* row found (ordered by id) wins and the rest are
silently dropped -- an acceptable lossy edge for a downgrade path that exists
to prove the migration reversible, not to serve as a supported multi-instance
export.

**After this migration, the seven ``spaces`` columns are read by nothing and
written by nothing.** They are not dropped here -- that is task 6.10's job,
once the store built here has been proven equivalent -- so this migration is
additive only: a column that used to be the single source of truth becomes
inert, its data copied forward, but it stays in place and still holds
whatever the last write left in it.

Revision ID: 22e7e6ba10b3
Revises: 26478eb4f9dd
Create Date: 2026-08-01 14:32:59.144840

"""

from datetime import datetime, time, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "22e7e6ba10b3"
down_revision: Union[str, Sequence[str], None] = "26478eb4f9dd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _spaces_table() -> sa.Table:
    """A lightweight, unmanaged reflection of ``spaces`` for Core-only access.

    Migrations in this repository are plain SQLAlchemy Core, never the ORM
    models -- see ``26478eb4f9dd`` for the precedent -- so the seven old
    columns are named again here rather than imported from
    ``app.identity.models.Space``.
    """
    metadata = sa.MetaData()
    return sa.Table(
        "spaces",
        metadata,
        sa.Column("id", sa.Integer),
        sa.Column("opens_at", sa.Time),
        sa.Column("closes_at", sa.Time),
        sa.Column("slot_minutes", sa.Integer),
        sa.Column("max_duration_minutes", sa.Integer),
        sa.Column("booking_horizon_days", sa.Integer),
        sa.Column("max_bookings_per_week", sa.Integer),
        sa.Column("max_bookings_per_month", sa.Integer),
    )


def _space_rules_table() -> sa.Table:
    metadata = sa.MetaData()
    return sa.Table(
        "space_rules",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("space_id", sa.Integer),
        sa.Column("rule_type", sa.String(length=64)),
        sa.Column("params", postgresql.JSONB),
        sa.Column("applies_to", postgresql.JSONB),
        sa.Column("enabled", sa.Boolean),
        sa.Column("created_at", UtcDateTime),
        sa.Column("updated_at", UtcDateTime),
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "space_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("rule_type", sa.String(length=64), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("applies_to", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_space_rules_space", "space_rules", ["space_id"], unique=False)

    _backfill_rows_from_columns()


def _backfill_rows_from_columns() -> None:
    """One ``space_rules`` row per non-null group of the old columns, per Space.

    See the module docstring for exactly which column(s) produce which
    ``rule_type`` and the param names each one is stored under -- the param
    names come from ``rules.registry.REGISTRY``, not from the column names,
    and the two disagree for ``booking_horizon`` and both counting rules.
    """
    connection = op.get_bind()
    spaces = _spaces_table()
    space_rules = _space_rules_table()

    now = datetime.now(timezone.utc)
    rows_to_insert: list[dict] = []

    for space in connection.execute(sa.select(spaces)).fetchall():
        if space.opens_at is not None and space.closes_at is not None:
            rows_to_insert.append(
                _new_row(
                    space.id,
                    "availability_hours",
                    {
                        "opens_at": space.opens_at.isoformat(),
                        "closes_at": space.closes_at.isoformat(),
                    },
                    now,
                )
            )

        if space.slot_minutes is not None:
            rows_to_insert.append(
                _new_row(space.id, "slot_alignment", {"slot_minutes": space.slot_minutes}, now)
            )

        if space.max_duration_minutes is not None:
            rows_to_insert.append(
                _new_row(
                    space.id,
                    "max_duration",
                    {"max_duration_minutes": space.max_duration_minutes},
                    now,
                )
            )

        if space.booking_horizon_days is not None:
            # `days`, not `booking_horizon_days` -- the registry's own param
            # name for `booking_horizon`.
            rows_to_insert.append(
                _new_row(space.id, "booking_horizon", {"days": space.booking_horizon_days}, now)
            )

        if space.max_bookings_per_week is not None:
            # `max_bookings`, not `max_bookings_per_week` -- the registry's
            # own param name.
            rows_to_insert.append(
                _new_row(
                    space.id,
                    "max_bookings_per_week",
                    {"max_bookings": space.max_bookings_per_week},
                    now,
                )
            )

        if space.max_bookings_per_month is not None:
            rows_to_insert.append(
                _new_row(
                    space.id,
                    "max_bookings_per_month",
                    {"max_bookings": space.max_bookings_per_month},
                    now,
                )
            )

    if rows_to_insert:
        connection.execute(sa.insert(space_rules), rows_to_insert)


def _new_row(space_id: int, rule_type: str, params: dict, now: datetime) -> dict:
    return {
        "space_id": space_id,
        "rule_type": rule_type,
        "params": params,
        "applies_to": None,
        "enabled": True,
        "created_at": now,
        "updated_at": now,
    }


def downgrade() -> None:
    """Downgrade schema."""
    _restore_columns_from_rows()

    op.drop_index("ix_space_rules_space", table_name="space_rules")
    op.drop_table("space_rules")


def _restore_columns_from_rows() -> None:
    """Rebuild the seven ``spaces`` columns from ``space_rules`` rows.

    The exact reverse of ``_backfill_rows_from_columns``: one column (or
    column pair) written back per ``rule_type``, reading the same param names
    the upgrade wrote. Grouped by ``space_id`` in Python rather than with one
    query per Space, since the whole table is being read anyway.
    """
    connection = op.get_bind()
    spaces = _spaces_table()
    space_rules = _space_rules_table()

    by_space: dict[int, dict[str, dict]] = {}
    rows = connection.execute(
        sa.select(space_rules).order_by(space_rules.c.space_id, space_rules.c.id)
    ).fetchall()
    for row in rows:
        # First row of a given type, for a given Space, wins -- see the module
        # docstring on why a second instance (not producible by the upgrade
        # above, but not prevented by the schema either) is a lossy edge here
        # rather than a supported export.
        by_space.setdefault(row.space_id, {}).setdefault(row.rule_type, row.params)

    for space_id, rules_by_type in by_space.items():
        values: dict = {}

        hours = rules_by_type.get("availability_hours")
        if hours is not None:
            values["opens_at"] = time.fromisoformat(hours["opens_at"])
            values["closes_at"] = time.fromisoformat(hours["closes_at"])

        slot_alignment = rules_by_type.get("slot_alignment")
        if slot_alignment is not None:
            values["slot_minutes"] = slot_alignment["slot_minutes"]

        max_duration = rules_by_type.get("max_duration")
        if max_duration is not None:
            values["max_duration_minutes"] = max_duration["max_duration_minutes"]

        booking_horizon = rules_by_type.get("booking_horizon")
        if booking_horizon is not None:
            values["booking_horizon_days"] = booking_horizon["days"]

        max_bookings_per_week = rules_by_type.get("max_bookings_per_week")
        if max_bookings_per_week is not None:
            values["max_bookings_per_week"] = max_bookings_per_week["max_bookings"]

        max_bookings_per_month = rules_by_type.get("max_bookings_per_month")
        if max_bookings_per_month is not None:
            values["max_bookings_per_month"] = max_bookings_per_month["max_bookings"]

        if values:
            connection.execute(sa.update(spaces).where(spaces.c.id == space_id).values(**values))
