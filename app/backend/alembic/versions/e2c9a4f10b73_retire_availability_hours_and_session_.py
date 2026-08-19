"""retire availability_hours and session_length rule types

The calendar shape (``.claude/rules/calendar-shape.md``) says everything these
two rule types said, as one document a calendar can be **drawn** from rather
than two predicates that can only answer yes or no about a booking already
proposed. Leaving them registered would leave an admin two places to configure
hours that can disagree, which is the state Stream 10 exists to end
(``ops/plans/stream-10/OVERVIEW.md``, decision 2). Both types left
``rules.REGISTRY`` and ``rules.canon`` in the same change; this migration is
its data half.

**It is load-bearing, not housekeeping.** A ``space_rules`` row naming a
``rule_type`` the catalog cannot resolve denies every booking at that Space
with ``RULE_ERROR_MESSAGE`` -- the fail-closed path an unregistered type has
always taken (``.claude/rules/rule-engine.md``). Leaving either type's rows
behind would therefore take every Space holding one offline, rather than
merely leaving stale data around. ``d1f4a9c7b2e0`` retired ``slot_alignment``
and ``min_duration`` for exactly this reason and this migration mirrors it.

**Nothing is derived from these rows on the way out.** No shape is built from
a Space's ``availability_hours`` or ``session_length`` values: there is no
production data to derive one from (OVERVIEW decision 3, settled by the repo
owner on 2026-08-17), so a derivation would have been written for zero rows,
tested against fixtures invented to exercise it, and deleted unread. Every
Space already holds a live shape row -- ``c07aeccce98c`` created the table and
backfilled ``DEFAULT_SHAPE`` onto every pre-existing Space -- so a Space whose
hours these rows expressed is left open 00:00-24:00 offering 60-minute
bookings, which is what a Space with neither of these rule types already
rendered as.

**Downgrade cannot reconstruct these rows and does not try.** Their values no
longer exist to be read, and re-seeding invented ones would fabricate hours no
admin configured. This is the same accepted lossy edge ``d1f4a9c7b2e0`` and
``c4679acf34aa`` both document: the downgrade path exists so
``test_migrations.py``'s full upgrade-then-downgrade-to-base cycle has
something correct to unwind, not to serve as a round-trip-safe export.

There is no schema change here. ``space_rules.rule_type`` is a plain string
column, so retiring a rule type is entirely a matter of the rows naming it.

Revision ID: e2c9a4f10b73
Revises: c07aeccce98c
Create Date: 2026-08-19 09:14:02.117845

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2c9a4f10b73"
down_revision: Union[str, Sequence[str], None] = "c07aeccce98c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The two rule types the calendar shape replaced, retired on 2026-08-18.
RETIRED_RULE_TYPES = ("availability_hours", "session_length")


def _space_rules_table() -> sa.Table:
    """A lightweight, unmanaged reflection of ``space_rules`` for Core-only access.

    Migrations in this repository are plain SQLAlchemy Core, never the ORM
    models -- see ``22e7e6ba10b3`` for the precedent this mirrors.
    """
    metadata = sa.MetaData()
    return sa.Table(
        "space_rules",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("rule_type", sa.String(length=64)),
    )


def upgrade() -> None:
    """Upgrade schema."""
    space_rules = _space_rules_table()
    op.get_bind().execute(
        sa.delete(space_rules).where(space_rules.c.rule_type.in_(RETIRED_RULE_TYPES))
    )


def downgrade() -> None:
    """Downgrade schema.

    Deliberately empty. See the module docstring: the deleted rows' parameters
    are gone, and inventing replacements would fabricate opening hours nobody
    configured.
    """
