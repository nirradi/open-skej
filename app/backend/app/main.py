from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth.dependencies import get_current_user
from app.auth.jwt import AuthError
from app.identity.models import User
from app.routers import bookings

# The Vite dev server. Stream 2 owns the real deployed origins; until then this
# is an explicit allowlist rather than "*" so credentialed requests keep working
# unchanged once auth lands.
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

app = FastAPI(title="Open-Skej")

app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bookings.router)


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
