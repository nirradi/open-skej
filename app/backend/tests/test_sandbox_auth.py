"""Sandbox auth mode: the negative controls, and the point of this suite.

Sandbox mode promotes ``test_auth_jwt.py``'s in-process-keypair fixture into a
*runtime* mode, so Playwright and manual QA can authenticate without a hosted
Auth0 login. Test code becoming runtime code is exactly how an auth bypass
ships, so the tests below are not incidental coverage — they are the
deliverable, proven as four separate controls, each asserting its own thing:

* Sandbox mode cannot be enabled against a real Auth0 config (raises).
* A sandbox-signed token does not leak into a normally-configured backend
  (rejected).
* With neither Auth0 nor sandbox configured, verification still fails closed,
  exactly as before, and ``/sandbox/token`` does not exist.
* A positive control: sandbox mode, switched on, actually works end to end —
  without this, the three controls above could be passing against a sandbox
  that never functions at all, which would prove nothing.

``_sandbox_settings`` is the one place environment variables are mutated.
Every test that uses it restores them (and clears both ``lru_cache``s) in a
``finally``, so a failed assertion never leaves ``SANDBOX_AUTH`` enabled for
whatever test runs next in the same process.
"""

import contextlib
import importlib
import os
from typing import Iterator

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth.jwt import AuthError, TokenVerifier, get_token_verifier
from app.auth.sandbox import mint_sandbox_token
from app.db.models import Base
from app.db.session import get_session
from app.main import app
from app.settings import get_settings

DATABASE_URL = os.environ.get("DATABASE_URL")

# Bound once, at module import (collection time) — before any test below has
# had a chance to mutate settings or reload `app.main`. This is the actual
# production app object, built under whatever sandbox-off environment the
# test process started with, and it is never affected by another test's
# `importlib.reload(app.main)` (that rebinds `app.main.app`, a module
# attribute, not this name already bound to the original object).
client = TestClient(app)

_ENV_KEYS = ("SANDBOX_AUTH", "AUTH0_DOMAIN", "AUTH0_API_AUDIENCE")


@contextlib.contextmanager
def _sandbox_settings(**env: str) -> Iterator[None]:
    """Set exactly these auth-config env vars for the block, then restore them.

    Any of ``SANDBOX_AUTH`` / ``AUTH0_DOMAIN`` / ``AUTH0_API_AUDIENCE`` not
    passed is unset for the duration, so a test asserts against a known
    combination rather than whatever a developer's ``.env`` happens to hold.
    Both settings' and the verifier's ``lru_cache`` are cleared going in —
    otherwise a cached ``Settings`` or ``TokenVerifier`` from an earlier test
    would silently outlive the environment change — and again on the way out,
    which is what stops a leak into the next test.
    """
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        for key in _ENV_KEYS:
            if key in env:
                os.environ[key] = env[key]
            else:
                os.environ.pop(key, None)
        get_settings.cache_clear()
        get_token_verifier.cache_clear()
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        get_token_verifier.cache_clear()


class _StubKey:
    """Mimics ``PyJWK``, which exposes the key material as ``.key``."""

    def __init__(self, key: object) -> None:
        self.key = key


class _FixedKeyResolver:
    """A real-tenant-shaped JWKS stub: always resolves to one known public key.

    Deliberately a *separate* class from anything in ``app.auth.sandbox`` —
    the whole point of this fixture is to model a verifier that has never
    heard of the sandbox key at all, the way a real Auth0-configured backend
    would not have.
    """

    def __init__(self, public_key: object) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> _StubKey:
        return _StubKey(self._public_key)


REAL_DOMAIN = "real-tenant.us.auth0.com"
REAL_AUDIENCE = "https://api.open-skej.dev"


# --- Negative control 1: sandbox cannot enable against a real Auth0 config. --


def test_sandbox_cannot_enable_against_a_real_auth0_config() -> None:
    """Guardrail 4, proven directly: the mutual-exclusion check.

    A backend with ``SANDBOX_AUTH`` on *and* a real tenant configured would
    trust a token signed by either the sandbox key or the tenant's JWKS —
    exactly the bypass this mode exists to avoid shipping. Construction must
    refuse rather than silently prefer one config over the other.
    """
    with _sandbox_settings(
        SANDBOX_AUTH="true",
        AUTH0_DOMAIN=REAL_DOMAIN,
        AUTH0_API_AUDIENCE=REAL_AUDIENCE,
    ):
        with pytest.raises(RuntimeError, match="SANDBOX_AUTH"):
            get_token_verifier()


# --- Negative control 2: a sandbox token does not leak into production. -----


def test_a_sandbox_signed_token_is_rejected_by_a_real_auth0_verifier() -> None:
    """A sandbox-minted token must not pass a normally-configured backend.

    Builds a verifier the way a real deployment would — its own domain,
    audience, and a JWKS resolver for its own key — with no reference to
    sandbox mode at all, and proves a token minted by the sandbox's *different*
    key is rejected rather than merely unfamiliar. This is the "does not leak
    into production" proof, and it needs no environment variables: it is true
    regardless of how the current process happens to be configured.
    """
    real_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    real_verifier = TokenVerifier(
        domain=REAL_DOMAIN,
        audience=REAL_AUDIENCE,
        jwks_client=_FixedKeyResolver(real_key.public_key()),
    )

    sandbox_token = mint_sandbox_token(sub="auth0|sandbox-user", email="qa@example.com")

    with pytest.raises(AuthError):
        real_verifier.verify(sandbox_token)


# --- Negative control 3: off by default, and fails closed. ------------------


def test_off_by_default_fails_closed() -> None:
    """With neither switch set, verification fails exactly as it always has.

    Mirrors ``test_auth_jwt.test_config_errors_are_not_auth_errors`` and
    extends it: sandbox mode does not change the "neither is configured"
    outcome, because sandbox is opt-in and this block never opts in. Read
    together with the mutual-exclusion test above, this is "one switch, both
    directions" — off changes nothing, and on-plus-real-Auth0 is refused.
    """
    with _sandbox_settings():
        settings = get_settings()
        assert settings.sandbox_auth is False, "sandbox_auth must default to False"

        with pytest.raises(RuntimeError, match="AUTH0_DOMAIN"):
            get_token_verifier()


def test_the_sandbox_route_does_not_exist_when_sandbox_is_off() -> None:
    """``/sandbox/token`` is a genuine 404 on a normally-configured backend.

    Not a 403: a 403 would mean the route exists and merely refused the
    caller, which would confirm sandbox mode is present but locked — exactly
    the oracle this task's registration guard exists to avoid. ``app`` here is
    the real production app object built under this process's actual (sandbox
    off) settings, not a stand-in rebuilt for the test.
    """
    response = client.post("/sandbox/token", json={"sub": "auth0|someone"})
    assert response.status_code == 404


# --- Positive control: sandbox mode, switched on, actually works. -----------


def test_positive_control_a_sandbox_token_is_accepted_when_sandbox_is_on() -> None:
    """Without this, every control above could be passing against a sandbox
    mode that never actually verifies anything — a verifier that rejected
    every token, sandbox-minted or not, would satisfy both negative controls
    while being useless. This pins the other end down.
    """
    with _sandbox_settings(SANDBOX_AUTH="true"):
        verifier = get_token_verifier()
        token = mint_sandbox_token(sub="auth0|qa-1", email="qa@example.com", email_verified=True)

        claims = verifier.verify(token)

        assert claims["sub"] == "auth0|qa-1"
        assert claims["email"] == "qa@example.com"
        assert claims["email_verified"] is True


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL is unset; this test needs `docker compose up -d`",
)
def test_positive_control_the_sandbox_endpoint_works_end_to_end() -> None:
    """The whole path a real Playwright run would take, through real wiring.

    ``POST /sandbox/token`` for an identity, then use the returned bearer
    token against an ordinary authenticated route (``/me``) with no dependency
    overridden except the database session. This is deliberately not a stub:
    it rebuilds ``app.main`` under ``SANDBOX_AUTH=true`` — via
    ``importlib.reload``, restored afterwards — which is the only way to prove
    ``main.py``'s conditional ``include_router`` line actually registers the
    route, rather than merely existing in the diff, and that
    ``get_current_user``'s real ``Depends(get_token_verifier)`` chain accepts
    what the endpoint mints.
    """
    engine = create_engine(DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    import app.main as main_module

    try:
        with _sandbox_settings(SANDBOX_AUTH="true"):
            importlib.reload(main_module)
            session: Session = factory()
            main_module.app.dependency_overrides[get_session] = lambda: session
            try:
                sandbox_client = TestClient(main_module.app)

                token_response = sandbox_client.post(
                    "/sandbox/token",
                    json={
                        "sub": "auth0|qa-e2e",
                        "email": "qa-e2e@example.com",
                        "email_verified": True,
                    },
                )
                assert token_response.status_code == 200
                access_token = token_response.json()["access_token"]

                me_response = sandbox_client.get(
                    "/me", headers={"Authorization": f"Bearer {access_token}"}
                )
            finally:
                main_module.app.dependency_overrides.clear()
                session.close()
        # Back outside `_sandbox_settings`: the environment and both caches
        # are restored to whatever they were before this test, i.e. sandbox
        # off. Reloading again here rebuilds `app.main.app` to match, so any
        # later code that imports the module fresh sees the ordinary app
        # rather than the sandbox-on one this test just built.
        importlib.reload(main_module)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()

    assert me_response.status_code == 200
    assert me_response.json()["email"] == "qa-e2e@example.com"
