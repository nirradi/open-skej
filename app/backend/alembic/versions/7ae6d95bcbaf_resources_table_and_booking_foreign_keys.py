"""resources table and booking foreign keys

The domain-model change: a Space is a **venue** holding many Resources, a Resource
is the bookable calendar, and a booking is against a real Resource made by a real
user. This revision adds the ``resources`` table and ``spaces.timezone`` (the
venue's IANA zone), and turns ``bookings.resource_id`` / ``bookings.user_id`` from
Stream 1's free-text ``String(64)`` placeholders into real foreign keys onto
``resources.id`` and ``users.id``.

Neither foreign key carries ``ON DELETE CASCADE``: nothing in this schema is
deleted, and a cascade would quietly destroy booking history when a Resource or a
user was removed.

**The booking column retype is not a plain ``ALTER``.** ``resource_id`` is used by
the ``ex_bookings_confirmed_no_overlap`` exclusion constraint and the
``ix_bookings_resource_status_start`` index, and Postgres refuses to change the
type of a column an exclusion constraint is defined on. So both are dropped, the
columns are retyped with an explicit ``USING`` cast (``varchar`` has no implicit
cast to ``integer``), the foreign keys are added, and the index and constraint are
recreated — the constraint identical but for the column now being an integer,
which ``btree_gist`` handles for the ``WITH =`` term exactly as it did the text.

Revision ID: 7ae6d95bcbaf
Revises: caafdd41ff19
Create Date: 2026-07-23 07:36:18.187971

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.db.models import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "7ae6d95bcbaf"
down_revision: Union[str, Sequence[str], None] = "caafdd41ff19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OVERLAP_CONSTRAINT = "ex_bookings_confirmed_no_overlap"
_RESOURCE_INDEX = "ix_bookings_resource_status_start"
_FK_RESOURCE = "fk_bookings_resource_id_resources"
_FK_USER = "fk_bookings_user_id_users"

# The overlap invariant, recreated identically after the retype: no two confirmed
# bookings on one Resource may cover overlapping half-open intervals.
_CREATE_OVERLAP = (
    f"ALTER TABLE bookings ADD CONSTRAINT {_OVERLAP_CONSTRAINT} "
    "EXCLUDE USING gist (resource_id WITH =, tsrange(start_at, end_at) WITH &&) "
    "WHERE (status = 'confirmed')"
)
_DROP_OVERLAP = f"ALTER TABLE bookings DROP CONSTRAINT {_OVERLAP_CONSTRAINT}"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "resources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("opens_at", sa.Time(), nullable=True),
        sa.Column("closes_at", sa.Time(), nullable=True),
        sa.Column("slot_minutes", sa.Integer(), nullable=True),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_resources_space", "resources", ["space_id"], unique=False)

    op.add_column(
        "spaces",
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default=sa.text("'UTC'"),
            nullable=False,
        ),
    )

    # Drop the objects that depend on bookings.resource_id, retype both id
    # columns, wire the foreign keys, then rebuild the index and the constraint.
    op.execute(_DROP_OVERLAP)
    op.drop_index(_RESOURCE_INDEX, table_name="bookings")
    op.alter_column(
        "bookings",
        "resource_id",
        existing_type=sa.VARCHAR(length=64),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="resource_id::integer",
    )
    op.alter_column(
        "bookings",
        "user_id",
        existing_type=sa.VARCHAR(length=64),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="user_id::integer",
    )
    op.create_foreign_key(_FK_RESOURCE, "bookings", "resources", ["resource_id"], ["id"])
    op.create_foreign_key(_FK_USER, "bookings", "users", ["user_id"], ["id"])
    op.create_index(
        _RESOURCE_INDEX,
        "bookings",
        ["resource_id", "status", "start_at"],
        unique=False,
    )
    op.execute(_CREATE_OVERLAP)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DROP_OVERLAP)
    op.drop_index(_RESOURCE_INDEX, table_name="bookings")
    op.drop_constraint(_FK_USER, "bookings", type_="foreignkey")
    op.drop_constraint(_FK_RESOURCE, "bookings", type_="foreignkey")
    op.alter_column(
        "bookings",
        "user_id",
        existing_type=sa.Integer(),
        type_=sa.VARCHAR(length=64),
        existing_nullable=False,
        postgresql_using="user_id::varchar",
    )
    op.alter_column(
        "bookings",
        "resource_id",
        existing_type=sa.Integer(),
        type_=sa.VARCHAR(length=64),
        existing_nullable=False,
        postgresql_using="resource_id::varchar",
    )
    op.create_index(
        _RESOURCE_INDEX,
        "bookings",
        ["resource_id", "status", "start_at"],
        unique=False,
    )
    op.execute(_CREATE_OVERLAP)

    op.drop_column("spaces", "timezone")
    op.drop_index("ix_resources_space", table_name="resources")
    op.drop_table("resources")
