"""availability hours store minutes not clock times

Task 7.10 rewrote ``AvailabilityHoursRule`` (``rules/rules/canon.py``) to compare
``context.local.start_minutes``/``end_minutes`` against two plain integers --
``opens_at_minutes``/``closes_at_minutes``, counted from the venue's own local
midnight -- instead of a pair of UTC ``time`` clock values resolved per booking
date through the (now-deleted) ``app.operating_hours.resolve_operating_hours``.
This migration is the data half of that rewrite: every existing
``availability_hours`` row's ``params`` moves from
``{"opens_at": "HH:MM:SS", "closes_at": "HH:MM:SS"}`` to
``{"opens_at_minutes": <int>, "closes_at_minutes": <int>}``.

**Upgrade** parses each row's stored ``opens_at``/``closes_at`` strings with
``time.fromisoformat`` and replaces ``params`` wholesale with the two computed
minute counts -- there is no third key on an ``availability_hours`` row today
(``identity-and-access.md``: both bounds are required together, and nothing
else is stored alongside them), so a wholesale replacement loses nothing.
JSONB values are not arithmetic-expressible in a single UPDATE ... SET
expression here, so this reads every matching row into Python, computes the
new ``params`` dict, and issues one ``UPDATE`` per row -- the same shape
``22e7e6ba10b3`` already established for backfilling this same table.

**Downgrade** reverses it: for every ``availability_hours`` row, it reads the
two ``*_minutes`` integers and writes back
``{"opens_at": time(...).isoformat(), "closes_at": time(...).isoformat()}``.
Both bounds are reduced with ``minutes % 1440`` before being turned into a
``time`` -- ``opens_at_minutes`` is always already below 1440 by construction
(``AvailabilityHoursRule.__init__`` enforces ``0 <= opens_at_minutes < 1440``),
so this only actually changes anything for a ``closes_at_minutes`` at or past
1440: a window crossing local midnight, newly representable only *after* this
migration's upgrade half has run. Reducing such a row to a bare wall-clock
``time`` on downgrade is an accepted lossy edge, not a supported export of a
state the pre-7.10 schema could never hold in the first place -- the same
posture ``22e7e6ba10b3``'s own downgrade already takes for a multi-instance
Space no upgrade of its own could produce. This downgrade path exists so
``test_migrations.py``'s full upgrade-then-downgrade-to-base cycle has
something correct to reconstruct for every row the upgrade itself could ever
have produced, not to serve as a round-trip-safe representation of every row
an admin could write after upgrading.

Revision ID: c4679acf34aa
Revises: c41d8e2b7f09
Create Date: 2026-08-07 20:19:02.555486

"""

from datetime import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4679acf34aa"
down_revision: Union[str, Sequence[str], None] = "c41d8e2b7f09"
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


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()
    space_rules = _space_rules_table()

    rows = connection.execute(
        sa.select(space_rules.c.id, space_rules.c.params).where(
            space_rules.c.rule_type == "availability_hours"
        )
    ).fetchall()

    for row in rows:
        opens_at = time.fromisoformat(row.params["opens_at"])
        closes_at = time.fromisoformat(row.params["closes_at"])
        new_params = {
            "opens_at_minutes": opens_at.hour * 60 + opens_at.minute,
            "closes_at_minutes": closes_at.hour * 60 + closes_at.minute,
        }
        connection.execute(
            sa.update(space_rules).where(space_rules.c.id == row.id).values(params=new_params)
        )


def downgrade() -> None:
    """Downgrade schema."""
    connection = op.get_bind()
    space_rules = _space_rules_table()

    rows = connection.execute(
        sa.select(space_rules.c.id, space_rules.c.params).where(
            space_rules.c.rule_type == "availability_hours"
        )
    ).fetchall()

    for row in rows:
        opens_at_minutes = row.params["opens_at_minutes"] % 1440
        closes_at_minutes = row.params["closes_at_minutes"] % 1440
        new_params = {
            "opens_at": time(opens_at_minutes // 60, opens_at_minutes % 60).isoformat(),
            "closes_at": time(closes_at_minutes // 60, closes_at_minutes % 60).isoformat(),
        }
        connection.execute(
            sa.update(space_rules).where(space_rules.c.id == row.id).values(params=new_params)
        )
