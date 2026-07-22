"""Application settings, read from the environment or a local ``.env``.

The real database and the Auth0 integration are both configured entirely through
environment variables. ``DATABASE_URL`` is the one Postgres connection string for
the whole backend — booking storage and identity share it, behind one engine and
one session factory. (Stream 1's separate ``SKEJ_DATABASE_URL`` / SQLite file is
retired; the drivers are unified on Postgres.)

Every field is optional so importing this module never raises. A missing
``DATABASE_URL`` is what the Postgres-only tests skip on, and the Auth0 values
are unset until task 2.3.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Auth0 M2M credentials already live in .env for the provisioning script
        # (task 2.4); without this, those keys would fail validation here.
        extra="ignore",
    )

    database_url: str | None = None
    auth0_domain: str | None = None
    auth0_api_audience: str | None = None

    # The Vite dev server's default origin. A list rather than a single value so
    # task 2.8's frontend can be served from a second port without a code change.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, built on first use.

    Cached so the ``.env`` file is read once rather than per request. Tests that
    need to vary the environment call ``get_settings.cache_clear()``.
    """
    return Settings()
