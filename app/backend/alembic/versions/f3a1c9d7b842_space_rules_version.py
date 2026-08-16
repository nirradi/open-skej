"""spaces.rules_version

Adds ``spaces.rules_version``, a monotonically increasing integer bumped by
``app.identity.service`` on every write to one of a Space's own ``space_rules``
rows (create, update, delete). Nothing in this schema reads it today; it exists
solely as the invalidation key ``app.projection_cache`` needs to know when a
cached rules-only projection for a Space has stopped being correct -- see that
module's own docstring, and ``app/backend/app/projection.py`` / ``POC.md`` for
the feature this supports.

``server_default='1'`` so an existing Space backfills to a real starting value
instead of ``NULL``, the same pattern ``d1f4a9c7b2e0`` and every other
NOT-NULL-column migration in this history already follows -- no Python-side
backfill loop is needed since every row gets the identical default.

Downgrade just drops the column; nothing else in this schema references it, so
there is no companion data migration to reverse.

Revision ID: f3a1c9d7b842
Revises: d1f4a9c7b2e0
Create Date: 2026-08-16 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a1c9d7b842"
down_revision: Union[str, Sequence[str], None] = "d1f4a9c7b2e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "spaces",
        sa.Column("rules_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("spaces", "rules_version")
