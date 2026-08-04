"""drop space schedule columns, shim retired

The contract half of an expand-then-contract migration (``ops/plans/stream-6-
plan.md``, task 6.10; task 6.6, ``22e7e6ba10b3``, is the expand half). The
seven ``spaces`` columns that migration backfilled into ``space_rules`` rows
-- and that nothing has read or written since -- are dropped outright:
``opens_at``, ``closes_at``, ``slot_minutes``, ``max_duration_minutes``,
``booking_horizon_days``, ``max_bookings_per_week``, ``max_bookings_per_month``.
``timezone`` is untouched -- it stays a genuinely per-Space column, never a
rule instance (``app.identity.models.Space``).

**The downgrade re-adds all seven columns, nullable, and repopulates them
from the current ``space_rules`` rows** -- the identical reconstruction
``22e7e6ba10b3.downgrade()`` performs, duplicated here rather than imported,
matching that migration's own precedent of naming Core-only table
reflections locally instead of reaching into ``app.identity.models``.
``test_migrations.py`` drives a full upgrade-then-downgrade-to-base cycle, and
``22e7e6ba10b3``'s own downgrade runs immediately after this one and writes
into the columns this migration re-adds -- so a downgrade that left them
unpopulated, or missing outright, would fail that cycle even though the two
migrations individually look reversible.

Revision ID: 2b3a230ee9e1
Revises: 22e7e6ba10b3
Create Date: 2026-08-04 07:58:14.859916

"""

from datetime import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "2b3a230ee9e1"
down_revision: Union[str, Sequence[str], None] = "22e7e6ba10b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _spaces_table() -> sa.Table:
    """A lightweight, unmanaged reflection of ``spaces`` for Core-only access.

    Migrations in this repository are plain SQLAlchemy Core, never the ORM
    models -- matching ``22e7e6ba10b3``'s own precedent -- so the seven old
    columns are named again here rather than imported from
    ``app.identity.models.Space``, which by this revision no longer declares
    them at all.
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
    op.drop_column("spaces", "opens_at")
    op.drop_column("spaces", "closes_at")
    op.drop_column("spaces", "slot_minutes")
    op.drop_column("spaces", "max_duration_minutes")
    op.drop_column("spaces", "booking_horizon_days")
    op.drop_column("spaces", "max_bookings_per_week")
    op.drop_column("spaces", "max_bookings_per_month")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("spaces", sa.Column("opens_at", sa.Time(), nullable=True))
    op.add_column("spaces", sa.Column("closes_at", sa.Time(), nullable=True))
    op.add_column("spaces", sa.Column("slot_minutes", sa.Integer(), nullable=True))
    op.add_column("spaces", sa.Column("max_duration_minutes", sa.Integer(), nullable=True))
    op.add_column("spaces", sa.Column("booking_horizon_days", sa.Integer(), nullable=True))
    op.add_column("spaces", sa.Column("max_bookings_per_week", sa.Integer(), nullable=True))
    op.add_column("spaces", sa.Column("max_bookings_per_month", sa.Integer(), nullable=True))

    _restore_columns_from_rows()


def _restore_columns_from_rows() -> None:
    """Rebuild the seven ``spaces`` columns from ``space_rules`` rows.

    The identical reconstruction ``22e7e6ba10b3._restore_columns_from_rows``
    performs: one column (or column pair) written back per ``rule_type``,
    reading the same param names the original upgrade wrote. Grouped by
    ``space_id`` in Python rather than with one query per Space, since the
    whole table is being read anyway.
    """
    connection = op.get_bind()
    spaces = _spaces_table()
    space_rules = _space_rules_table()

    by_space: dict[int, dict[str, dict]] = {}
    rows = connection.execute(
        sa.select(space_rules).order_by(space_rules.c.space_id, space_rules.c.id)
    ).fetchall()
    for row in rows:
        # First row of a given type, for a given Space, wins -- see
        # ``22e7e6ba10b3``'s module docstring on why a second instance is a
        # lossy edge here rather than a supported export.
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
