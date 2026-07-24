"""Sandbox-only login: mint a token for a requested identity.

Registered by ``app.main`` **only when ``settings.sandbox_auth`` is true** —
see that module's conditional ``include_router``. That conditional
registration is the whole of this endpoint's access control: with sandbox
mode off, the route does not exist, so a caller against a normally-configured
backend gets a genuine 404 for ``POST /sandbox/token``, not a 403 that would
first require the route to exist in order to refuse it. There is deliberately
no dependency here re-checking ``sandbox_auth`` per request — the guardrail
lives at registration, once, rather than at every call site that could
forget it.

Seeded identities arrive in a later task; this endpoint mints a token for
whatever ``sub`` / ``email`` / ``email_verified`` it is handed, which is
enough for that task (and Playwright, after it) to obtain a real,
independently-verifiable token without also needing a JWT-signing library of
its own.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.auth.sandbox import mint_sandbox_token

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


class SandboxTokenRequest(BaseModel):
    sub: str
    email: str | None = None
    # Defaults to verified: the common QA case is a fully-provisioned member,
    # and a test that specifically needs the unverified-invitation path can
    # still ask for it explicitly.
    email_verified: bool = True


class SandboxTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/token", response_model=SandboxTokenResponse)
def issue_sandbox_token(body: SandboxTokenRequest) -> SandboxTokenResponse:
    """Mint a sandbox-signed token carrying the given identity.

    No credential is checked against the request body — this endpoint *is*
    the credential-issuing step for sandbox mode, and its security boundary is
    that it exists at all only when sandbox mode is on. See the module
    docstring.
    """
    token = mint_sandbox_token(
        sub=body.sub,
        email=body.email,
        email_verified=body.email_verified,
    )
    return SandboxTokenResponse(access_token=token)
