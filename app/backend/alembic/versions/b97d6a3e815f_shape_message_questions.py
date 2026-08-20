"""persist shape-assistant clarification questions

The shape studio must recover an unfinished unbookable turn from the product
database, even after browser storage has been cleared. Its assistant summary is
display copy, while the nullable clarification question controls both the safe
live-preview fallback and the explicit unbookable publish acknowledgement, so it
is stored as a first-class transcript field rather than reconstructed from prose.

Revision ID: b97d6a3e815f
Revises: a4c2e7f190d3
Create Date: 2026-08-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b97d6a3e815f"
down_revision: Union[str, Sequence[str], None] = "a4c2e7f190d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the durable nullable question to visible shape messages."""
    op.add_column("space_shape_messages", sa.Column("question", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove the recovery-only transcript field."""
    op.drop_column("space_shape_messages", "question")
