"""generated rule types table

Task 7.6 (``ops/plans/stream-7/7.6-generated-rule-types-and-the-catalog.md``). ``rules.REGISTRY``
holds seven hand-written rule types; this table is where a type produced by the AI generation
loop lives once a run succeeds — a ``space_rules`` row can then name it exactly like any
registered type, once ``app.rule_catalog`` has hoisted it into the process's live catalog (see
that module for the load path).

Global, not Space-scoped — a generated type is available to every Space, matching the seven
hand-written ones (``ops/plans/stream-7/OVERVIEW.md``, "Generated rules are global, with
provenance recorded"). ``created_by_space_id`` (NOT NULL) and ``created_by_user_id`` record who
made it, so per-Space scoping later is a migration over columns that already hold the answer
rather than an archaeology exercise.

Nothing writes a row here yet — that is task 7.7's job. This migration only opens the table;
``app.identity.models.GeneratedRuleType`` carries the full column-by-column reasoning, including
why ``python_version`` and ``bytecode_magic`` are both stored and why ``status`` is retire-not-
delete rather than a contradiction of this schema's "nothing is deleted" convention.

Revision ID: 1657bbf3450c
Revises: 2b3a230ee9e1
Create Date: 2026-08-06 10:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "1657bbf3450c"
down_revision: Union[str, Sequence[str], None] = "2b3a230ee9e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "generated_rule_types",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_type", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("human_code", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("executable_bytecode", sa.LargeBinary(), nullable=False),
        sa.Column("python_version", sa.String(length=32), nullable=False),
        sa.Column("bytecode_magic", sa.String(length=16), nullable=False),
        sa.Column(
            "param_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("reads_history", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "retired",
                name="generated_rule_type_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("created_by_space_id", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_space_id"], ["spaces.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_type", name="uq_generated_rule_types_rule_type"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("generated_rule_types")
