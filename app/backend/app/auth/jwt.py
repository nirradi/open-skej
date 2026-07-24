"""Auth0 access-token verification.

Auth0 signs its access tokens with a rotating RSA key and publishes the public
half at ``https://{domain}/.well-known/jwks.json``. Verification therefore has
three moving parts: fetch the right public key by ``kid``, check the signature,
and check the claims (``aud``, ``iss``, ``exp``, ``nbf``).

Two failure modes drive most of the shape of this module:

* **Algorithm confusion.** A verifier that trusts the token's own ``alg`` header
  can be handed an ``alg: none`` token, or an HS256 token whose HMAC secret is
  the tenant's *public* key — which is, by definition, public. Both are then
  "valid". The defence is an explicit allowlist, applied here twice: the header
  is rejected up front if it names anything but RS256, and ``jwt.decode`` is
  given the same one-element ``algorithms`` list so the check survives even if
  the header pre-check is ever refactored away.
* **A 500 where a 401 belongs.** Every rejection below raises :class:`AuthError`
  and nothing else, so ``main.py`` can map one exception type to 401. A
  ``PyJWTError`` escaping to the default handler would be a 500, which tells a
  caller "we broke" when the truth is "your token is bad" — and it makes the
  failure invisible to any client that only branches on 401.

Configuration errors are deliberately *not* :class:`AuthError`: a missing
``AUTH0_DOMAIN`` is a broken deployment, not a bad credential, and dressing it
up as 401 would send every user to a login screen that cannot help them.
"""

from functools import lru_cache
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient, PyJWKClientError

from app.settings import get_settings

# The only algorithm Auth0 issues for RS256-configured APIs, and the only one we
# accept. A list of one, never widened at runtime.
ALLOWED_ALGORITHMS = ["RS256"]

# Tolerance for clock skew between the Auth0 tenant and this host when checking
# `exp` and `nbf`. Small on purpose: it is the window in which an expired token
# still works, so it buys correctness against NTP drift and nothing more.
CLOCK_SKEW_LEEWAY_SECONDS = 30

# How long a fetched JWKS stays usable before it is re-fetched. Auth0 rotates
# signing keys rarely, and PyJWKClient re-fetches on an unknown `kid` anyway, so
# a long lifespan costs nothing and keeps a tenant outage from taking down
# verification of tokens we already have the key for.
JWKS_CACHE_SECONDS = 600

# Claims every Auth0 access token carries. Requiring them means a token that
# simply omits `exp` cannot slip past the expiry check by having nothing to
# check — PyJWT skips absent claims silently.
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub"]


class AuthError(Exception):
    """A bearer token was not accepted, for any reason.

    One type for every rejection, so the API surfaces 401 uniformly. ``detail``
    is safe to return to the caller: it names the *kind* of failure ("token has
    expired") without echoing token contents back.
    """

    def __init__(self, detail: str = "Not authenticated") -> None:
        super().__init__(detail)
        self.detail = detail


class SigningKeyResolver(Protocol):
    """The slice of ``PyJWKClient`` this module depends on.

    Narrowed to one method so tests can supply a stub keyed off an in-process
    JWKS without standing up an HTTP server or reaching the real tenant.
    """

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class TokenVerifier:
    """Verifies access tokens for one Auth0 tenant and one API audience."""

    def __init__(
        self,
        domain: str,
        audience: str,
        jwks_client: SigningKeyResolver | None = None,
    ) -> None:
        self.domain = domain
        self.audience = audience
        # Auth0's `iss` always carries the trailing slash. Without it every
        # token fails issuer validation, which is a confusing way to discover a
        # typo, so it is appended here rather than expected from configuration.
        self.issuer = f"https://{domain}/"
        self.jwks_uri = f"https://{domain}/.well-known/jwks.json"
        self._jwks_client: SigningKeyResolver = jwks_client or PyJWKClient(
            self.jwks_uri,
            cache_keys=True,
            lifespan=JWKS_CACHE_SECONDS,
        )

    def verify(self, token: str) -> dict[str, Any]:
        """Return the verified claims, or raise :class:`AuthError`."""
        self._require_allowed_algorithm(token)

        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        except PyJWKClientError as exc:
            # No JWKS entry matches the token's `kid` — the token was signed by
            # a key this tenant does not publish.
            raise AuthError("Token was signed by an unknown key") from exc
        except jwt.PyJWTError as exc:
            raise AuthError("Token is malformed") from exc

        try:
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Token has expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise AuthError("Token is not valid yet") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("Token was issued for a different audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("Token was issued by an unknown issuer") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AuthError("Token is missing a required claim") from exc
        except jwt.InvalidSignatureError as exc:
            raise AuthError("Token signature is invalid") from exc
        # Anything else PyJWT can raise — a bad algorithm, an undecodable
        # payload — is still a rejected token, never a server fault.
        except jwt.PyJWTError as exc:
            raise AuthError("Token is invalid") from exc

    @staticmethod
    def _require_allowed_algorithm(token: str) -> None:
        """Reject the token before its signature is even looked at.

        This is the algorithm-confusion gate. ``alg: none`` and an HS256 token
        signed with the tenant's public key both die here, on the header alone,
        without the key-lookup or signature paths ever being asked to have an
        opinion about them.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("Token is malformed") from exc

        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise AuthError("Token uses an unsupported signing algorithm")


@lru_cache(maxsize=1)
def get_token_verifier() -> TokenVerifier:
    """The process-wide verifier, built on first use.

    Cached so the JWKS is fetched once per process rather than once per request.
    Tests that vary the Auth0 settings call ``get_token_verifier.cache_clear()``.

    This is also where sandbox auth mode (``SANDBOX_AUTH=true``, see
    ``app.auth.sandbox``) is wired in, and where its guardrails are enforced —
    at the one place a verifier for the running process comes into existence:

    * **Mutual exclusion first, before anything else runs.** A backend with
      the sandbox switch on *and* a real Auth0 tenant configured would trust a
      token signed by either the sandbox key or the tenant's JWKS — that is
      the auth bypass this whole mode exists to avoid shipping, so it is
      refused loudly rather than one of the two configs silently winning.
    * **Sandbox is never inferred.** A backend with neither Auth0 nor the
      sandbox switch configured still fails exactly as it always has — the
      branch below only ever *adds* a mode, it never treats an absent Auth0
      config as permission to fall back to the sandbox key.
    """
    settings = get_settings()
    real_auth0_configured = bool(settings.auth0_domain) and bool(settings.auth0_api_audience)

    if settings.sandbox_auth and real_auth0_configured:
        raise RuntimeError(
            "SANDBOX_AUTH is enabled together with AUTH0_DOMAIN / AUTH0_API_AUDIENCE. "
            "Sandbox mode trusts a local keypair instead of the real tenant; a backend "
            "trusting both would accept a token signed by either. Unset one before starting — "
            "sandbox mode is for a deployment with no real Auth0 tenant configured at all."
        )

    if settings.sandbox_auth:
        # Local import: `app.auth.sandbox` imports `TokenVerifier` from this
        # module, so importing it at module scope here would be a cycle.
        from app.auth.sandbox import build_sandbox_verifier

        return build_sandbox_verifier()

    if not real_auth0_configured:
        raise RuntimeError(
            "AUTH0_DOMAIN and AUTH0_API_AUDIENCE must be set to verify access tokens, or "
            "SANDBOX_AUTH=true for a local sandbox keypair. Run scripts/auth0_provision.py "
            "and copy the printed values into .env."
        )
    return TokenVerifier(
        domain=settings.auth0_domain,
        audience=settings.auth0_api_audience,
    )
