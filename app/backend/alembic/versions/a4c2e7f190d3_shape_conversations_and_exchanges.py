"""shape conversations and recorded exchanges

Shape authoring is a short synchronous conversation, but it still changes the calendar that
members can book.  The transcript and each model exchange are therefore retained in the product
database.  ``prompt_versions`` remains the one sha256-keyed store for system prompts; this
migration adds ``shape`` to its agent check rather than creating a second prompt store.

Only one conversation may be open in a Space.  As with rule-generation jobs, a partial unique
index is the enforcement: two browser tabs can both observe no open conversation, but only one
may insert one.  Closed conversations, their messages, and their exchanges are retained.

Revision ID: a4c2e7f190d3
Revises: e2c9a4f10b73
Create Date: 2026-08-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.models import UtcDateTime

revision: str = "a4c2e7f190d3"
down_revision: Union[str, Sequence[str], None] = "e2c9a4f10b73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _prompt_agent_enum() -> sa.Enum:
    return sa.Enum(
        "generator",
        "tester",
        "manifest",
        "shape",
        name="prompt_agent",
        native_enum=False,
        create_constraint=True,
        length=16,
    )


def upgrade() -> None:
    """Upgrade schema."""
    # The original non-native enum created a CHECK named after its type on each table.  Widen both
    # before a new prompt version can name the shape agent.
    for table in ("prompt_versions", "rule_generation_exchanges"):
        op.drop_constraint("prompt_agent", table, type_="check")
        op.create_check_constraint(
            "prompt_agent",
            table,
            "agent IN ('generator', 'tester', 'manifest', 'shape')",
        )

    op.create_table(
        "space_shape_conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "closed",
                name="space_shape_conversation_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("closed_at", UtcDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_space_shape_conversations_space", "space_shape_conversations", ["space_id"])
    op.create_index(
        "uq_space_shape_conversations_open",
        "space_shape_conversations",
        ["space_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

    op.create_table(
        "space_shape_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "user",
                "assistant",
                name="space_shape_message_role",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("resulting_shape_version_id", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["space_shape_conversations.id"]),
        sa.ForeignKeyConstraint(
            ["resulting_shape_version_id"], ["space_calendar_shapes.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "ordinal", name="uq_space_shape_messages_conversation_ordinal"
        ),
    )

    op.create_table(
        "space_shape_exchanges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("prompt_version_id", sa.Integer(), nullable=False),
        sa.Column("user_prompt", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "completed",
                "failed",
                name="space_shape_exchange_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["space_shape_conversations.id"]),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["prompt_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_space_shape_exchanges_conversation_id", "space_shape_exchanges", ["conversation_id"]
    )
    op.create_foreign_key(
        "fk_space_calendar_shapes_source_conversation",
        "space_calendar_shapes",
        "space_shape_conversations",
        ["source_conversation_id"],
        ["id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "fk_space_calendar_shapes_source_conversation", "space_calendar_shapes", type_="foreignkey"
    )
    op.drop_index("ix_space_shape_exchanges_conversation_id", table_name="space_shape_exchanges")
    op.drop_table("space_shape_exchanges")
    op.drop_table("space_shape_messages")
    op.drop_index("uq_space_shape_conversations_open", table_name="space_shape_conversations")
    op.drop_index("ix_space_shape_conversations_space", table_name="space_shape_conversations")
    op.drop_table("space_shape_conversations")
    # Shape exchanges are gone, so no remaining row may legitimately reference this prompt-agent
    # value.  Delete those prompt versions before narrowing the old CHECK back to its three values.
    op.get_bind().execute(sa.text("DELETE FROM prompt_versions WHERE agent = 'shape'"))
    for table in ("rule_generation_exchanges", "prompt_versions"):
        op.drop_constraint("prompt_agent", table, type_="check")
        op.create_check_constraint(
            "prompt_agent", table, "agent IN ('generator', 'tester', 'manifest')"
        )
