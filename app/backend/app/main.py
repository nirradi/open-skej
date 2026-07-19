from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Hello World"}
