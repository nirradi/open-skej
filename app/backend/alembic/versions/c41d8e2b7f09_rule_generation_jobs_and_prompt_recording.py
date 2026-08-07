"""rule generation jobs, prompt versions and exchanges

Task 7.7 (``ops/plans/stream-7/7.7-generation-as-a-job.md``). Generation is generate →
adversarial suite → sandbox run with up to three retries: minutes of wall clock and up to eight
model calls, which cannot be the body of an HTTP request. ``rule_generation_jobs`` is the row an
admin submits a prompt against and then polls; ``app.rule_generation`` runs it in-process and
``app.routers.rule_drafts`` is the API over it.

The other two tables are the recording. There is no LangChain, no LangGraph and no tracing SDK in
this project and none is being added (``ops/plans/stream-7/OVERVIEW.md`` argues that at length),
so without these tables every prompt this feature sends would be lost the moment the call
returned — including the retry turn, where ``build_prompt`` hands the model back its own failing
source plus the validator or pytest output verbatim. That turn is the whole prompt-debugging
surface and the only evidence of why a rule now enforcing a venue's bookings reads the way it
does.

``prompt_versions`` stores a system prompt **once, keyed on its hash**, rather than verbatim on
every exchange: the Generator's is ~5.7 kB against up to eight calls per generation. Size is the
smaller half of it — the join it gives, which system-prompt version produced which outcome, is
the question prompt tuning actually asks and a pile of near-identical copies cannot answer.

The partial unique index on ``rule_generation_jobs`` covers in-flight rows only
(``status IN ('queued', 'running')``), the same shape as ``uq_space_access_requests_pending``: at
most one live job per Space, and no constraint at all on finished ones, so a Space that generated
yesterday can generate again today. ``app.identity.models`` carries the column-by-column
reasoning, including why ``attempts`` does not duplicate the rule source and why the three metric
columns are nullable rather than zero-defaulted.

Revision ID: c41d8e2b7f09
Revises: 1657bbf3450c
Create Date: 2026-08-07 09:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "c41d8e2b7f09"
down_revision: Union[str, Sequence[str], None] = "1657bbf3450c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _prompt_agent_enum() -> sa.Enum:
    """The ``PromptAgent`` column type, built twice below and identical both times."""
    return sa.Enum(
        "generator",
        "tester",
        "manifest",
        name="prompt_agent",
        native_enum=False,
        create_constraint=True,
        length=16,
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rule_generation_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                name="rule_generation_job_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "attempts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("generated_rule_type_id", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["generated_rule_type_id"], ["generated_rule_types.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rule_generation_jobs_space_id", "rule_generation_jobs", ["space_id"])
    # In-flight rows only. See the module docstring: uniqueness over the live rows is what lets a
    # Space generate again tomorrow while stopping it queueing five at once today.
    op.create_index(
        "uq_rule_generation_jobs_in_flight",
        "rule_generation_jobs",
        ["space_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("agent", _prompt_agent_enum(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("first_seen_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256", name="uq_prompt_versions_sha256"),
    )

    op.create_table(
        "rule_generation_exchanges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("agent", _prompt_agent_enum(), nullable=False),
        sa.Column("prompt_version_id", sa.Integer(), nullable=False),
        sa.Column("user_prompt", sa.Text(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["rule_generation_jobs.id"]),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["prompt_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rule_generation_exchanges_job_id", "rule_generation_exchanges", ["job_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_rule_generation_exchanges_job_id", table_name="rule_generation_exchanges")
    op.drop_table("rule_generation_exchanges")
    op.drop_table("prompt_versions")
    op.drop_index("uq_rule_generation_jobs_in_flight", table_name="rule_generation_jobs")
    op.drop_index("ix_rule_generation_jobs_space_id", table_name="rule_generation_jobs")
    op.drop_table("rule_generation_jobs")
