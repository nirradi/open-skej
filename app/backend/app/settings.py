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

    # Sandbox auth mode: signs and verifies tokens against an in-process keypair
    # instead of the real Auth0 tenant, for Playwright and manual QA with no
    # hosted login. Off by default and never inferred from a missing Auth0
    # value — see `app.auth.jwt.get_token_verifier` for the mutual-exclusion
    # guard this switch is paired with, and `app.auth.sandbox` for the rest.
    sandbox_auth: bool = False

    # AI rule generation. Off by default and, exactly like `sandbox_auth` above,
    # never inferred from a key being present: whether the capability is even
    # available should not be discoverable by whether a request to it succeeds,
    # which is why `app.main` registers the router conditionally rather than
    # guarding inside the handler. The second reason is narrower and just as
    # real — an unconfigured backend must not be able to spend money on model
    # calls, so enabling it is a deliberate act rather than a side effect of
    # having set `GOOGLE_STUDIO_API_KEY` for the benchmark.
    rule_generation_enabled: bool = False
    # `stub` | `google` | `ollama` | `claude-cli`. The default is `stub`
    # deliberately: an enabled-but-otherwise-unconfigured backend then runs the
    # whole flow against a canned response instead of billing anyone.
    rule_generation_client: str = "stub"
    google_studio_api_key: str | None = None
    # None means "leave the selected client's own default model alone", the same
    # convention `rules/benchmark.py`'s optional flags already keep.
    rule_generation_model: str | None = None
    # A shape turn is synchronous: unlike a rule-generation job it may never hold a request open
    # for the transport's benchmark-sized default timeout. The configured client still owns socket
    # cancellation; this is the shorter bound applied to that client for shape conversations only.
    shape_conversation_timeout_seconds: float = Field(default=30.0, gt=0)

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
