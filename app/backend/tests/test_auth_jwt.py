"""Token-verification tests, run against an in-process RSA keypair.

No network and no Auth0 credentials: the keypair is generated here and handed to
``TokenVerifier`` as a stub JWKS resolver, so this suite passes in CI with no
secrets configured. Only the provisioning script and a real browser login ever
touch the live tenant.

Every rejection below is a **separate test asserting its own reason**. A single
"a bad token raises AuthError" test would pass just as happily against a verifier
that rejected every token including the valid ones, which would prove nothing at
all. The valid-token test is what pins the other end down.
"""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.jwt import AuthError, TokenVerifier


def _b64url(raw: bytes) -> str:
    """Base64url without padding, as JWS requires."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _epoch_in(seconds: int) -> int:
    return int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp())


DOMAIN = "test-tenant.us.auth0.com"
ISSUER = f"https://{DOMAIN}/"
AUDIENCE = "https://api.open-skej.dev"
SUBJECT = "auth0|abc123"


def _generate_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return _generate_key()


@pytest.fixture(scope="module")
def other_key() -> rsa.RSAPrivateKey:
    """A second keypair the verifier has never heard of."""
    return _generate_key()


class _StubKey:
    """Mimics ``PyJWK``, which exposes the key material as ``.key``."""

    def __init__(self, key: object) -> None:
        self.key = key


class _StubResolver:
    """Stands in for ``PyJWKClient``, always returning one known public key."""

    def __init__(self, public_key: object) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> _StubKey:
        # The real client raises when no JWKS entry matches the token's `kid`.
        # Here the stub always resolves, which means a token signed by
        # `other_key` reaches `jwt.decode` and dies on the *signature* instead —
        # a stricter path through the code than a lookup miss would be.
        return _StubKey(self._public_key)


@pytest.fixture
def verifier(private_key: rsa.RSAPrivateKey) -> TokenVerifier:
    return TokenVerifier(
        domain=DOMAIN,
        audience=AUDIENCE,
        jwks_client=_StubResolver(private_key.public_key()),
    )


def _claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    claims: dict[str, object] = {
        "sub": SUBJECT,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "email": "user@example.com",
        "email_verified": True,
    }
    claims.update(overrides)
    return claims


def _token(private_key: rsa.RSAPrivateKey, **overrides: object) -> str:
    return jwt.encode(_claims(**overrides), private_key, algorithm="RS256")


# --- The positive case, which stops every test below being vacuous. ---------


def test_a_valid_token_is_accepted(verifier: TokenVerifier, private_key: rsa.RSAPrivateKey) -> None:
    claims = verifier.verify(_token(private_key))

    assert claims["sub"] == SUBJECT
    assert claims["email"] == "user@example.com"


# --- Claim validation. ------------------------------------------------------


def test_an_expired_token_is_rejected(
    verifier: TokenVerifier, private_key: rsa.RSAPrivateKey
) -> None:
    now = datetime.now(timezone.utc)
    token = _token(private_key, iat=now - timedelta(hours=2), exp=now - timedelta(hours=1))

    with pytest.raises(AuthError, match="expired"):
        verifier.verify(token)


def test_a_token_for_another_audience_is_rejected(
    verifier: TokenVerifier, private_key: rsa.RSAPrivateKey
) -> None:
    token = _token(private_key, aud="https://api.someone-else.example")

    with pytest.raises(AuthError, match="audience"):
        verifier.verify(token)


def test_a_token_from_another_issuer_is_rejected(
    verifier: TokenVerifier, private_key: rsa.RSAPrivateKey
) -> None:
    """A token from a *different Auth0 tenant* — correctly signed, wrong origin.

    Worth its own case because such a token is otherwise entirely well-formed;
    only the issuer distinguishes it.
    """
    token = _token(private_key, iss="https://attacker-tenant.us.auth0.com/")

    with pytest.raises(AuthError, match="issuer"):
        verifier.verify(token)


def test_a_token_missing_a_required_claim_is_rejected(
    verifier: TokenVerifier, private_key: rsa.RSAPrivateKey
) -> None:
    """A token with no ``exp`` must not sail past the expiry check.

    PyJWT skips absent claims silently, so without the explicit ``require``
    option an unexpiring token would verify cleanly and never age out.
    """
    claims = _claims()
    del claims["exp"]
    token = jwt.encode(claims, private_key, algorithm="RS256")

    with pytest.raises(AuthError, match="required claim"):
        verifier.verify(token)


# --- Signature and algorithm. -----------------------------------------------


def test_a_token_signed_by_an_unknown_key_is_rejected(
    verifier: TokenVerifier, other_key: rsa.RSAPrivateKey
) -> None:
    with pytest.raises(AuthError):
        verifier.verify(_token(other_key))


def test_alg_none_is_rejected(verifier: TokenVerifier) -> None:
    """The unsigned-token attack: strip the signature and claim it is fine."""
    token = jwt.encode(_claims(), key=None, algorithm="none")

    with pytest.raises(AuthError, match="unsupported signing algorithm"):
        verifier.verify(token)


def test_hs256_signed_with_the_rsa_public_key_is_rejected(
    verifier: TokenVerifier, private_key: rsa.RSAPrivateKey
) -> None:
    """The classic algorithm-confusion attack, and the reason for the allowlist.

    The tenant's public key is, by definition, public. A verifier that trusts the
    token's own ``alg`` header would treat that public key as an HMAC shared
    secret and accept a token any reader of the JWKS endpoint could mint. The
    forged token here is well-formed and correctly HMAC'd — it is rejected purely
    because RS256 is the only algorithm on the allowlist.
    """
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Assembled by hand rather than with `jwt.encode`, which refuses a PEM key as
    # an HMAC secret. That refusal is a guard on the *signing* side; an attacker
    # is under no obligation to use PyJWT to forge, so the token is built the way
    # they would build it. Anything less would be testing PyJWT's encoder rather
    # than our verifier.
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(_claims(exp=_epoch_in(3600), iat=_epoch_in(0))).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
    forged = f"{header}.{payload}.{signature}"

    # The forgery is genuinely well-formed: the header really does claim HS256,
    # and the signature really is a correct HMAC over the signing input under the
    # public key. Asserted directly rather than via `jwt.decode`, because PyJWT
    # refuses a PEM key as an HMAC secret on the decode side too.
    assert json.loads(base64.urlsafe_b64decode(header + "=="))["alg"] == "HS256"
    expected = _b64url(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
    assert signature == expected

    # The specific message matters. Our allowlist rejects this on the header
    # alone, before the key is ever fetched or used — so "unsupported signing
    # algorithm" proves *our* gate fired. PyJWT's own InvalidKeyError guard would
    # surface as the generic "Token is invalid", which would mean we were leaning
    # on a library safeguard rather than on the allowlist this module documents.
    with pytest.raises(AuthError, match="unsupported signing algorithm"):
        verifier.verify(forged)


def test_a_malformed_token_is_rejected(verifier: TokenVerifier) -> None:
    with pytest.raises(AuthError, match="malformed"):
        verifier.verify("this-is-not-a-jwt")


def test_config_errors_are_not_auth_errors() -> None:
    """A missing tenant is a broken deployment, not a bad credential.

    Surfacing it as 401 would send every user to a login screen that cannot
    possibly help them, and would hide the outage from monitoring that watches
    5xx rates.
    """
    from app.auth.jwt import get_token_verifier
    from app.settings import get_settings

    get_settings.cache_clear()
    get_token_verifier.cache_clear()
    try:
        settings = get_settings()
        if settings.auth0_domain and settings.auth0_api_audience:
            pytest.skip("Auth0 is configured in this environment; nothing to assert")

        with pytest.raises(RuntimeError, match="AUTH0_DOMAIN"):
            get_token_verifier()
    finally:
        get_settings.cache_clear()
        get_token_verifier.cache_clear()
