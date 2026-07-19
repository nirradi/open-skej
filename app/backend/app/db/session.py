"""Postgres engine, session factory and the FastAPI session dependency.

This is Stream 2's entry point into the real database. Stream 1's SQLite driver
in ``app/db/sqlite.py`` builds its own engine and is untouched by anything here;
the two coexist until Stream 4 merges them.

The engine is built lazily rather than at import time so that importing this
module with no ``DATABASE_URL`` set is harmless — Stream 1's test suite imports
the package without a Postgres anywhere in sight.
"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.settings import get_settings


def _require_database_url() -> str:
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Start Postgres with `docker compose up -d` and export "
            "DATABASE_URL=postgresql+psycopg://skej:skej@localhost:5432/skej"
        )
    return url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """The process-wide engine, built on first use.

    Cached so every request shares one connection pool. ``pool_pre_ping`` costs a
    trivial round trip and avoids handing out a connection the server has already
    dropped, which is the usual failure after a compose restart.
    """
    return create_engine(_require_database_url(), pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session that is always closed.

    The ``finally`` matters: without it a handler that raises would leak its
    connection back to nothing, and the pool would drain under any sustained
    error. Committing is left to the caller so a handler can control its own
    transaction boundary.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
