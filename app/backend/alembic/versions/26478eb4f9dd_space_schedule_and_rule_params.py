"""space schedule and rule params

The domain-model correction: the Space, not the Resource, is the unit of
configuration and policy. A Resource is one of N indistinguishable courts — a
unit of bookable capacity carrying no configuration of its own. This revision
moves ``opens_at`` / ``closes_at`` / ``slot_minutes`` from ``resources`` to
``spaces``, and adds the four rule-engine parameters
(``max_duration_minutes``, ``booking_horizon_days``, ``max_bookings_per_week``,
``max_bookings_per_month``) to ``spaces``. All seven columns are nullable —
unset means "no restriction" / "rule not enforced" — so this is a plain
add/drop with no backfill: nothing is deployed and the sandbox seed resets on
every run.

The rule engine itself is unchanged by this migration. It still runs the
module-level ``DEFAULT_CANON`` (task 4.13b assembles a canon from these
columns); this PR only relocates storage.

Revision ID: 26478eb4f9dd
Revises: 7ae6d95bcbaf
Create Date: 2026-07-25 14:16:18.462537

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "26478eb4f9dd"
down_revision: Union[str, Sequence[str], None] = "7ae6d95bcbaf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("spaces", sa.Column("opens_at", sa.Time(), nullable=True))
    op.add_column("spaces", sa.Column("closes_at", sa.Time(), nullable=True))
    op.add_column("spaces", sa.Column("slot_minutes", sa.Integer(), nullable=True))
    op.add_column("spaces", sa.Column("max_duration_minutes", sa.Integer(), nullable=True))
    op.add_column("spaces", sa.Column("booking_horizon_days", sa.Integer(), nullable=True))
    op.add_column("spaces", sa.Column("max_bookings_per_week", sa.Integer(), nullable=True))
    op.add_column("spaces", sa.Column("max_bookings_per_month", sa.Integer(), nullable=True))

    op.drop_column("resources", "opens_at")
    op.drop_column("resources", "closes_at")
    op.drop_column("resources", "slot_minutes")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("resources", sa.Column("opens_at", sa.Time(), nullable=True))
    op.add_column("resources", sa.Column("closes_at", sa.Time(), nullable=True))
    op.add_column("resources", sa.Column("slot_minutes", sa.Integer(), nullable=True))

    op.drop_column("spaces", "max_bookings_per_month")
    op.drop_column("spaces", "max_bookings_per_week")
    op.drop_column("spaces", "booking_horizon_days")
    op.drop_column("spaces", "max_duration_minutes")
    op.drop_column("spaces", "slot_minutes")
    op.drop_column("spaces", "closes_at")
    op.drop_column("spaces", "opens_at")
