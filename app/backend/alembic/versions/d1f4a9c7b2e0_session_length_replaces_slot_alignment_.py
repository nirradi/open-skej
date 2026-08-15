"""session_length replaces slot_alignment and min_duration

``ops/pending/bugs/grid-from-hours-and-min-duration.md`` replaced two rule types
with one: ``session_length``, whose grid is anchored on the date's own resolved
opening time rather than on local midnight. ``slot_alignment`` and
``min_duration`` are both unregistered by the same change, and this migration is
its data half.

**It is load-bearing, not housekeeping.** A ``space_rules`` row naming a
``rule_type`` the catalog cannot resolve denies every booking at that Space with
``RULE_ERROR_MESSAGE`` -- the fail-closed path an unregistered type has always
taken (``.claude/rules/rule-engine.md``). Leaving either type's rows behind
would therefore take every Space seeded since Space creation started writing a
``slot_alignment`` row offline, rather than merely leaving stale data around.

**Upgrade converts ``slot_alignment`` in place and deletes ``min_duration``.**
The conversion is a rename of the type and of its one parameter key --
``slot_minutes`` becomes ``session_minutes`` -- keeping ``space_id``,
``applies_to`` and ``enabled`` exactly as they were. Converting rather than
deleting and re-seeding a flat default preserves the grid step an admin actually
chose, and every stored ``slot_minutes`` is already a valid ``session_minutes``
because ``SlotAlignmentRule`` enforced the identical ``1440 % n == 0`` bound that
``SessionLengthRule`` now enforces. What the conversion does change is meaning,
deliberately: the row's grid was anchored at local midnight and is now anchored
at that date's own opening time.

``min_duration`` rows are deleted outright and are not converted. A floor and a
grid step are different constraints -- a Space holding both a 30-minute grid and
a 45-minute floor would convert into two conflicting ``session_length`` rows --
and the floor no longer needs a rule of its own: with both bounds required to
land on the grid, the shortest booking that can exist is one session. A Space
that held a ``min_duration`` row and no ``slot_alignment`` row is left with no
session rule at all, which is the ordinary "not enforced" state every rule type
in this schema expresses as the absence of a row.

**Downgrade** reverses the conversion -- ``session_length`` rows become
``slot_alignment`` rows carrying ``slot_minutes`` again -- and cannot reconstruct
the deleted ``min_duration`` rows, which no longer exist to be read. That is an
accepted lossy edge of the same kind ``c4679acf34aa``'s own downgrade documents:
this path exists so ``test_migrations.py``'s full upgrade-then-downgrade-to-base
cycle has something correct to reconstruct for every row the upgrade itself
produced, not to serve as a round-trip-safe export of a state the pre-change
schema held.

There is no schema change here. ``space_rules.rule_type`` is a plain string
column, so retiring a rule type is entirely a matter of the rows naming it.

Revision ID: d1f4a9c7b2e0
Revises: c4679acf34aa
Create Date: 2026-08-15 10:12:41.883204

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d1f4a9c7b2e0"
down_revision: Union[str, Sequence[str], None] = "c4679acf34aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _space_rules_table() -> sa.Table:
    """A lightweight, unmanaged reflection of ``space_rules`` for Core-only access.

    Migrations in this repository are plain SQLAlchemy Core, never the ORM models --
    see ``22e7e6ba10b3`` for the precedent this mirrors.
    """
    metadata = sa.MetaData()
    return sa.Table(
        "space_rules",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("rule_type", sa.String(length=64)),
        sa.Column("params", postgresql.JSONB),
    )


def _rename_type_and_param(
    connection: sa.engine.Connection,
    space_rules: sa.Table,
    *,
    from_type: str,
    to_type: str,
    from_key: str,
    to_key: str,
) -> None:
    """Repoint every ``from_type`` row at ``to_type``, moving its one parameter key.

    ``params`` is replaced wholesale rather than merged: neither type stores a
    second key, so there is nothing else on the row to preserve.
    """
    rows = connection.execute(
        sa.select(space_rules.c.id, space_rules.c.params).where(
            space_rules.c.rule_type == from_type
        )
    ).fetchall()

    for row in rows:
        connection.execute(
            sa.update(space_rules)
            .where(space_rules.c.id == row.id)
            .values(rule_type=to_type, params={to_key: row.params[from_key]})
        )


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()
    space_rules = _space_rules_table()

    _rename_type_and_param(
        connection,
        space_rules,
        from_type="slot_alignment",
        to_type="session_length",
        from_key="slot_minutes",
        to_key="session_minutes",
    )

    connection.execute(sa.delete(space_rules).where(space_rules.c.rule_type == "min_duration"))


def downgrade() -> None:
    """Downgrade schema."""
    connection = op.get_bind()
    space_rules = _space_rules_table()

    _rename_type_and_param(
        connection,
        space_rules,
        from_type="session_length",
        to_type="slot_alignment",
        from_key="session_minutes",
        to_key="slot_minutes",
    )
