"""Autogenerate filter keeping Stream 2's migrations off Stream 1's tables.

Why this exists
---------------
``app/db/models.py`` defines a single ``Base``, and Stream 1's ``Booking`` model
is registered on it. Alembic's autogenerate compares *all* of ``Base.metadata``
against the database, so without a filter the very first ``alembic revision
--autogenerate`` would emit a ``CREATE TABLE bookings`` — claiming ownership of a
table Stream 1 is still actively changing across its own tasks. Any such
migration would go stale the moment Stream 1 altered the model, and the two
streams would then be fighting over the same schema from opposite directions.

Stream 1 owns ``bookings`` and creates it via ``Base.metadata.create_all`` on
SQLite. Stream 2 owns the identity tables (``users``, ``spaces``,
``space_memberships``, ...) and manages them with Alembic on Postgres. This hook
enforces that split mechanically instead of relying on anyone remembering to
hand-edit every generated migration.

Sharing a single ``Base`` is intentional — it keeps one metadata registry so
Stream 4's foreign keys resolve — which is exactly why the boundary has to live
here rather than in a second declarative base. Stream 4 removes this filter when
it folds ``bookings`` into Alembic, together with the ``EXCLUDE USING gist``
constraint that ``app/db/driver.py`` specifies.
"""

from typing import Any

# Tables Stream 1 owns. Anything listed here is invisible to autogenerate.
STREAM_1_OWNED_TABLES = frozenset({"bookings"})


def include_object(
    obj: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """Return False for anything belonging to a Stream 1 table.

    This is Alembic's ``include_object`` hook, wired up in ``alembic/env.py``.

    Excluding the table alone is not enough: its indexes, constraints and columns
    are offered to this hook as separate objects and would otherwise still be
    emitted, producing a migration that references a table the same migration
    never creates. Those objects carry no table name in ``name``, so the owning
    table is read from their ``.table`` attribute.
    """
    if type_ == "table":
        return name not in STREAM_1_OWNED_TABLES

    table = getattr(obj, "table", None)
    owning_table = getattr(table, "name", None) if table is not None else None
    return owning_table not in STREAM_1_OWNED_TABLES
