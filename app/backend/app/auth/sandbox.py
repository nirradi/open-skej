"""Sandbox auth mode: the JWT test fixture promoted to a runtime mode.

Playwright and manual QA need a deterministic identity with no hosted Auth0
login page. ``test_auth_jwt.py`` already generates an in-process RSA keypair
and hands ``TokenVerifier`` a stub JWKS resolver so that suite passes with no
Auth0 credentials anywhere in sight — this module is that same apparatus,
wired up so a *running* backend can sign and verify tokens against its own
key, not just a test process.

That promotion is exactly why this file is dangerous rather than merely
convenient. Test code becoming runtime code is how auth bypasses ship, so
three properties hold and are enforced by the caller, not by convention here:

* **Explicit opt-in only.** Nothing reaches this module unless
  ``settings.sandbox_auth`` is ``True``. There is no path from "Auth0 is
  unconfigured" to here — ``get_token_verifier`` treats that as a fail-closed
  configuration error, never as an invitation to try the sandbox key.
* **Mutually exclusive with the real tenant.** ``get_token_verifier`` refuses
  to build a sandbox verifier at all when ``auth0_domain`` / ``auth0_api_audience``
  are also set. A backend willing to accept either a sandbox-signed token or a
  real Auth0 one is the bypass this task exists to prevent.
* **A distinct issuer and audience.** Real Auth0 issuers are always
  ``https://{tenant}.us.auth0.com/``; the sandbox values below can never
  collide with one. Even if some future refactor ever pointed a real-Auth0
  verifier at a sandbox token by mistake, the issuer/audience checks already
  in ``TokenVerifier.verify`` would still reject it — this module adds no new
  trust path, it reuses the one ``jwt.py`` already enforces.

The keypair is generated **in-process, lazily, on first use**. A process
restart invalidates every outstanding sandbox token, which is the correct
failure mode for a sandbox — "log in again" costs nothing, and persisting an
unencrypted private key to disk would buy durability nobody asked for.
"""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.jwt import TokenVerifier

# Distinct from any real Auth0 issuer/audience by construction — see the
# module docstring's third bullet. Never derived from a request or from
# configuration, so there is nothing here for a caller to coerce into
# colliding with a real tenant's values.
SANDBOX_ISSUER = "https://sandbox.open-skej.local/"
SANDBOX_AUDIENCE = "https://sandbox.open-skej.local/api"

# `TokenVerifier.__init__` derives `self.issuer` as `f"https://{domain}/"`, so
# passing this as `domain` reproduces `SANDBOX_ISSUER` exactly. Building the
# sandbox verifier through the same constructor real-Auth0 verifiers use means
# there is exactly one place issuer construction happens, not a second one to
# keep in sync with the first.
_SANDBOX_DOMAIN = "sandbox.open-skej.local"

# Long enough that a Playwright run or a manual QA session never re-mints
# mid-session; short enough that a leaked sandbox token is not a standing
# credential. Not configurable — a sandbox has no production traffic pattern
# to tune this against.
SANDBOX_TOKEN_TTL_SECONDS = 3600


@lru_cache(maxsize=1)
def _keypair() -> rsa.RSAPrivateKey:
    """The process-wide sandbox key, generated on first use.

    Cached exactly like ``get_settings`` and ``get_token_verifier``: one key
    per process, not one per token or per request. Nothing persists it, so a
    process restart is a hard reset of every sandbox identity in flight.
    """
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _SandboxKeyResolver:
    """The runtime form of ``test_auth_jwt.py``'s ``_StubResolver``.

    Always resolves to the one sandbox public key, regardless of the token's
    ``kid`` — there is exactly one sandbox key per process, so there is
    nothing to look up by key id. A token signed by any other key still
    reaches ``jwt.decode`` and is rejected there, on the signature, rather
    than here on a lookup miss — the stricter of the two paths, and the one
    the test fixture this mirrors already documents preferring.
    """

    def get_signing_key_from_jwt(self, token: str) -> Any:
        class _ResolvedKey:
            key = _keypair().public_key()

        return _ResolvedKey()


def build_sandbox_verifier() -> TokenVerifier:
    """A ``TokenVerifier`` trusting only the sandbox key, issuer, and audience.

    Called from ``get_token_verifier`` and nowhere else. That function is what
    checks the opt-in and mutual-exclusion guardrails before ever reaching
    here; this function assumes they have already passed and only builds the
    verifier — it holds no policy of its own.
    """
    return TokenVerifier(
        domain=_SANDBOX_DOMAIN,
        audience=SANDBOX_AUDIENCE,
        jwks_client=_SandboxKeyResolver(),
    )


def mint_sandbox_token(
    sub: str,
    email: str | None = None,
    email_verified: bool = True,
) -> str:
    """Sign a token that only ``build_sandbox_verifier``'s verifier accepts.

    Carries exactly the claims ``get_current_user`` reads: ``sub`` for the
    just-in-time provisioning lookup, ``email`` / ``email_verified`` for the
    invitation-claiming gate. RS256, the sandbox key, the sandbox issuer and
    audience — the same algorithm allowlist and claim shape a real Auth0
    token must satisfy, because this reuses ``TokenVerifier.verify`` rather
    than bypassing it. A caller of this function chooses the identity; it does
    not choose the algorithm, the issuer, or the audience.
    """
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "sub": sub,
        "email": email or "",
        "email_verified": email_verified,
        "aud": SANDBOX_AUDIENCE,
        "iss": SANDBOX_ISSUER,
        "iat": now,
        "exp": now + timedelta(seconds=SANDBOX_TOKEN_TTL_SECONDS),
    }
    return jwt.encode(claims, _keypair(), algorithm="RS256")
