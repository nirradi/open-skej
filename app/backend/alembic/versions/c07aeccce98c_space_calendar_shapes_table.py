"""space calendar shapes table

Task 10.2 (``ops/plans/stream-10/10.2-a-shape-per-space.md``). A Space's calendar shape — what it
offers structurally, as opposed to a rule, which says who may take it (``.claude/rules/calendar-
shape.md``) — is versioned, not a single row per Space: ``space_calendar_shapes`` holds one row per
version, distinguished by ``status`` (``draft`` | ``live`` | ``superseded``), so a chat turn can
write a draft the preview reads while the venue keeps operating under the version it already
published.

Two partial unique indexes carry the whole "at most one live, at most one draft" constraint,
enforced by the database rather than by a read-then-write check in the service layer — the
identical shape and reasoning ``uq_rule_generation_jobs_in_flight`` already gives in
``c41d8e2b7f09_rule_generation_jobs_and_prompt_recording.py``: two concurrent writers can both pass
a check and only one can pass an index.

**Nothing is deleted, and no shape is derived.** ``OVERVIEW`` decision 3 (settled by the repo owner,
2026-08-17): nothing is deployed yet, so this migration does not attempt to read a Space's existing
``availability_hours`` / ``session_length`` rows and reconstruct a shape from them — that logic
would be written for zero real rows, tested against fixtures invented to exercise it, and deleted
unread. Instead every pre-existing Space is backfilled a single ``live`` row holding a literal copy
of ``DEFAULT_SHAPE`` (``rules/shape/types.py``): open every day 00:00-24:00, one offered duration of
60 minutes, no blackouts — which is what a Space with neither retired row already rendered as, so
this backfill changes no existing test's meaning. ``created_by_user_id`` and
``published_by_user_id`` are left ``NULL`` on every backfilled row: a system backfill has no acting
user, and both columns are nullable on this table for exactly that reason (see the model's own
docstring in ``app/identity/models.py``).

The literal below **must stay byte-for-byte identical to ``DEFAULT_SHAPE``.** No migration in this
history imports application code or the ``rules``/``shape`` packages — only ``app.db.models
.UtcDateTime``, a driver-level type — so the value cannot be imported here and is copied instead.
If ``DEFAULT_SHAPE`` ever changes, this literal does not follow it automatically and must be updated
by hand; it describes the version of the default that shipped at the time this migration ran, not
the version that exists today.

A Space created after this migration runs never reaches this backfill at all — ``create_space``
(``app/identity/service.py``) writes its own live default shape in the same transaction as the rest
of Space creation, so "a Space with no shape row" stays an unreachable state on both sides of this
migration.

Revision ID: c07aeccce98c
Revises: d1f4a9c7b2e0
Create Date: 2026-08-17 21:04:51.417291

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "c07aeccce98c"
down_revision: Union[str, Sequence[str], None] = "d1f4a9c7b2e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# A literal copy of ``rules/shape/types.py``'s ``DEFAULT_SHAPE`` — see the module docstring above
# for why this migration cannot import it and must instead keep this copy in sync by hand. Open
# every day, 00:00-24:00, one offered duration of 60 minutes, no blackouts.
_DEFAULT_SHAPE_DOCUMENT = {
    "version": 1,
    "operating_blocks": [
        {
            "days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
            "start_time": "00:00",
            "end_time": "24:00",
            "allowed_durations_mins": [60],
        }
    ],
    "blackout_windows": [],
}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "space_calendar_shapes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "live",
                "superseded",
                name="space_calendar_shape_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("published_at", UtcDateTime(), nullable=True),
        sa.Column("published_by_user_id", sa.Integer(), nullable=True),
        # No ForeignKey — task 10.8 adds the conversations table this will eventually reference.
        # See the module docstring and the model's own docstring.
        sa.Column("source_conversation_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_space_calendar_shapes_space", "space_calendar_shapes", ["space_id"])
    # At most one live shape per Space. See the module docstring: this is the same partial-unique-
    # index shape as `uq_rule_generation_jobs_in_flight`, enforced by the database rather than by a
    # read-then-write check.
    op.create_index(
        "uq_space_calendar_shapes_live",
        "space_calendar_shapes",
        ["space_id"],
        unique=True,
        postgresql_where=sa.text("status = 'live'"),
    )
    # At most one draft per Space — the identical reasoning, over the other status a Space may
    # hold at most one live instance of.
    op.create_index(
        "uq_space_calendar_shapes_draft",
        "space_calendar_shapes",
        ["space_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )

    # Backfill: every pre-existing Space gets one `live` row holding the default shape. See the
    # module docstring — OVERVIEW decision 3 is what rules out deriving a shape from any existing
    # `availability_hours` / `session_length` row instead.
    connection = op.get_bind()
    space_ids = connection.execute(sa.text("SELECT id FROM spaces")).scalars().all()
    if space_ids:
        shapes_table = sa.table(
            "space_calendar_shapes",
            sa.column("space_id", sa.Integer()),
            sa.column("document", postgresql.JSONB()),
            sa.column("status", sa.String()),
            sa.column("created_at", UtcDateTime()),
            sa.column("created_by_user_id", sa.Integer()),
            sa.column("published_at", UtcDateTime()),
            sa.column("published_by_user_id", sa.Integer()),
        )
        now = sa.func.now()
        connection.execute(
            shapes_table.insert(),
            [
                {
                    "space_id": space_id,
                    "document": _DEFAULT_SHAPE_DOCUMENT,
                    "status": "live",
                    "created_at": now,
                    "created_by_user_id": None,
                    "published_at": now,
                    "published_by_user_id": None,
                }
                for space_id in space_ids
            ],
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_space_calendar_shapes_draft", table_name="space_calendar_shapes")
    op.drop_index("uq_space_calendar_shapes_live", table_name="space_calendar_shapes")
    op.drop_index("ix_space_calendar_shapes_space", table_name="space_calendar_shapes")
    op.drop_table("space_calendar_shapes")
