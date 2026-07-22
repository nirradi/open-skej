"""Alembic environment for Stream 2's Postgres schema.

The database URL comes from ``app.settings.Settings`` (``DATABASE_URL``) rather
than from ``alembic.ini``, so there is one source of truth and no credentials in
the repo.

``target_metadata`` is the shared ``Base.metadata`` from ``app.db.models``.
Autogenerate compares it against the database and now manages *both* halves — the
identity tables and the ``bookings`` table — since Stream 4 folded booking
storage into Alembic. There is no table-scoping filter: one migration history
owns the whole schema.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.models import Base, UtcDateTime
from app.settings import get_settings

# Imported for their side effect: registering every model on ``Base.metadata``.
# Autogenerate compares the metadata against the database, so a model package
# that is never imported produces an empty migration rather than an error — a
# failure that is easy to miss. ``app.db.models`` (imported above for ``Base``)
# carries the ``Booking`` model; ``app.identity`` carries the identity tables.
# ``noqa: F401`` because the name itself is unused here.
import app.identity  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context) -> object:
    """Make autogenerate emit an import for our custom ``UtcDateTime`` type.

    Without this, a timestamp column renders as the bare text
    ``app.db.models.UtcDateTime()`` while the generated file imports only
    ``sqlalchemy`` — so the migration raises ``NameError: name 'app' is not
    defined`` the moment Alembic tries to load it. Every migration touching a
    timestamp would be born broken and need the same hand-fix, so the import is
    registered here once instead.

    Returning ``False`` falls back to Alembic's default rendering for everything
    else.
    """
    if type_ == "type" and isinstance(obj, UtcDateTime):
        autogen_context.imports.add("from app.db.models import UtcDateTime")
        return "UtcDateTime()"
    return False


def _database_url() -> str:
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Start Postgres with `docker compose up -d` and export "
            "DATABASE_URL=postgresql+psycopg://skej:skej@localhost:5432/skej"
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
        # Detect column type changes; off by default and easy to miss otherwise.
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the database and run migrations against it."""
    config.set_main_option("sqlalchemy.url", _database_url())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_item=render_item,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
