import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthError
from app.db.session import get_session_factory
from app.identity.models import User
from app.identity.router import router as spaces_router
from app.identity.router import rule_types_router
from app.rule_catalog import catalog
from app.routers import resource_bookings
from app.settings import get_settings

logger = logging.getLogger(__name__)

# The Vite dev server. Stream 2 owns the real deployed origins; until then this
# is an explicit allowlist rather than "*" so credentialed requests keep working
# unchanged once auth lands.
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Load every generated rule type once, before this process serves its first request.

    Without this, the catalog starts empty and only self-heals on a booking that misses
    (``app.rule_catalog.RuleCatalog.lookup``) — correct, but it would mean this process's very
    first booking against a generated type pays a database round trip that every one after it
    would not have to. Loading here instead means the common case is warm from the first request.

    Wrapped in its own ``try``/``except``: a backend that cannot reach the database at boot must
    still start — every booking then fails closed through the path that already exists for an
    unregistered ``rule_type`` (``app.rules_stub._UnbuildableRuleRowError``), which is a correct,
    already-tested outcome, rather than a backend that never comes up at all because Postgres
    happened to be slow to accept connections.
    """
    try:
        session = get_session_factory()()
        try:
            catalog.reload(session)
        finally:
            session.close()
    except Exception:
        logger.exception("Could not load the rule catalog at startup; continuing without it.")
    yield


app = FastAPI(title="Open-Skej", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(resource_bookings.router)
app.include_router(spaces_router)
app.include_router(rule_types_router)

if get_settings().sandbox_auth:
    # Conditional on purpose: `/sandbox/token` must not exist at all on a
    # normally-configured backend, so a caller there gets 404 rather than a
    # 403 that would first require the route to exist in order to refuse it.
    # See `app.routers.sandbox` and `app.auth.sandbox` for the rest of the
    # sandbox-auth-mode guardrails.
    from app.routers import sandbox as sandbox_router

    app.include_router(sandbox_router.router)


@app.exception_handler(AuthError)
def handle_auth_error(_request: Request, exc: AuthError) -> JSONResponse:
    """Map every token rejection to 401.

    Without this, an AuthError raised inside a dependency reaches FastAPI's
    default handler and becomes a 500 — telling the caller "we broke" when the
    truth is "your token is bad", and hiding the failure from any client that
    branches on 401 to trigger a re-login.

    ``WWW-Authenticate`` is required by RFC 7235 on a 401 and is what tells a
    client which scheme to retry with.
    """
    return JSONResponse(
        status_code=401,
        content={"detail": exc.detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Hello World"}


@app.get("/me")
def read_me(user: User = Depends(get_current_user)) -> dict[str, object]:
    """The authenticated caller.

    Doubles as the frontend's "is my token still good?" probe, which is why it
    does no work beyond what ``get_current_user`` already did.
    """
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }
