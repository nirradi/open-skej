# Open-Skej

A scheduling application for booking time on shared resources — a tennis court, an expensive piece of
lab equipment, anything with more demand than availability.

The differentiator is **AI-driven rule configuration**: booking constraints ("only 1 hour sessions",
"no more than twice a week") are stored as parameterized Python snippets and enforced by an isolated
rule engine, rather than hardcoded into the booking logic.

## Architecture

Work is split into three vertical streams that develop independently before a final integration phase.

| Stream | Scope | Status |
|---|---|---|
| **1 — Core E2E Booking** | Calendar UI, booking flow, SQLite persistence, stubbed rules and auth | Complete |
| **2 — Auth, Access & Admin** | Auth0, real Postgres schema, multi-tenant Spaces, admin dashboard | In progress |
| **3 — Rule Engine** | Isolated Python execution environment, AI rule generation + verification loop | Not started |

Repository layout:

```
app/backend    FastAPI service (Python) — booking endpoints, SQLite driver, stub rule engine
app/frontend   Vite + React 19 + Tailwind — the calendar grid and booking flow
app/e2e        Standalone Playwright suite; boots both servers itself
rules          Stream 3's rule engine package (currently a placeholder)
```

> **Stream 1 caveat:** authentication and the rule engine are deliberately stubbed. See
> [Current limitations](#current-limitations) before drawing conclusions about production behaviour.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Node.js | **22** | Pinned in `.github/workflows/ci.yml`. Verified locally on v22.22.0 / npm 10.9.4. |
| Python | **3.12** | Pinned in CI. No `.python-version` file exists; 3.14 also runs green locally — see the note below. |

<details>
<summary>Python version discrepancy (read if you hit an install error)</summary>

CI pins `python-version: "3.12"`. The virtualenvs at `app/backend/venv` and `rules/venv` are
gitignored — you create them yourself — and on the machine this runbook was written on they report
**Python 3.14.4** and run the full suite green.

Nothing in the project enforces a floor: there is no `.python-version`, no `setup.py`, and no
`requires-python` in either `pyproject.toml`. So both versions work today. If you are creating a
fresh venv, **prefer 3.12**, since that is the only version CI actually validates against.

</details>

## First-time setup

Each package installs independently. Run these from the repository root.

```bash
# Backend
python3 -m venv app/backend/venv
app/backend/venv/bin/pip install -r app/backend/requirements.txt

# Rule engine (Stream 3)
python3 -m venv rules/venv
rules/venv/bin/pip install -r rules/requirements.txt

# Frontend
npm ci --prefix app/frontend

# E2E suite
npm ci --prefix app/e2e
npx --prefix app/e2e playwright install chromium
```

The frontend reads its backend URL from `VITE_API_BASE_URL`, which **defaults to
`http://localhost:8000` when unset** — so for standard local development you do not need an env file
at all. To point at a different backend:

```bash
cp app/frontend/.env.example app/frontend/.env
```

## Running locally

Two processes, two terminals.

**Terminal 1 — backend:**

```bash
cd app/backend
./venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

**Terminal 2 — frontend:**

```bash
cd app/frontend
npm run dev
```

| What | URL |
|---|---|
| Calendar UI | <http://localhost:5173/> |
| API root | <http://localhost:8000/> |
| Interactive API docs (Swagger) | <http://localhost:8000/docs> |

Uvicorn binds to `127.0.0.1` by default; `localhost` and `127.0.0.1` both work. CORS is allowlisted
for `http://localhost:5173` and `http://127.0.0.1:5173` only (`app/backend/app/main.py`) — serving the
frontend from any other origin will fail preflight.

### Where the data lives

The backend writes to `./skej.db` relative to its working directory, i.e. `app/backend/skej.db`.
SQLite runs in WAL mode, so you will also see `skej.db-wal` and `skej.db-shm`. All three are
gitignored. Override the location with `SKEJ_DATABASE_URL` (see `app/backend/app/dependencies.py`):

```bash
SKEJ_DATABASE_URL="sqlite+pysqlite:///$(pwd)/scratch.db" ./venv/bin/python -m uvicorn app.main:app --port 8000
```

To reset your local data, stop the server and delete all three files.

## Seeding sample data

**There is no seed script.** The honest path is to POST against the running API.

Bookings must satisfy the stub rule engine (`app/backend/app/rules_stub.py`) or they are rejected with
a friendly message:

| Constraint | Value |
|---|---|
| Availability window | **06:00–23:00**, evaluated against the wall clock *as supplied* |
| Maximum duration | **2 hours** |
| Not in the past | `start_at` must be ≥ now |
| Booking horizon | at most **60 days** ahead |

Bounds are inclusive on the generous side: a booking ending exactly at 23:00 is accepted, as is one
starting exactly 60 days out.

This snippet creates a one-hour booking tomorrow at 10:00 UTC and prints the response:

```bash
cd app/backend
python3 - <<'PY'
import json, urllib.request
from datetime import datetime, timedelta, timezone

day = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
    minute=0, second=0, microsecond=0
)
body = json.dumps({
    "start_at": day.replace(hour=10).isoformat(),
    "end_at":   day.replace(hour=11).isoformat(),
}).encode()

req = urllib.request.Request(
    "http://localhost:8000/bookings", data=body,
    headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(req) as r:
    print(r.status, json.load(r))
PY
```

Expected output — `201` and the created booking:

```
201 {'id': 1, 'resource_id': 'default-resource', 'user_id': 'default-user',
     'start_at': '2026-07-20T10:00:00Z', 'end_at': '2026-07-20T11:00:00Z',
     'status': 'confirmed', 'created_at': '...', 'cancelled_at': None}
```

The request body takes only `start_at` and `end_at`. `resource_id` and `user_id` are hardcoded
constants (`default-resource` / `default-user`) until Stream 2 lands — sending them in the body is
**silently ignored**, not rejected, so don't expect an error if you try.

### Reading bookings back

`GET /bookings` requires **both** `from` and `to` query parameters; omitting either is a `422`.

> **Gotcha:** a `+00:00` offset in a query string is decoded as a space and fails validation. Use a
> `Z` suffix, or percent-encode the `+` as `%2B`.

```bash
curl "http://localhost:8000/bookings?from=2026-07-18T00:00:00Z&to=2026-07-26T00:00:00Z"
```

Add `&include_cancelled=true` to include soft-deleted rows (cancellation is a status change, not a
row delete, because Stream 3's history-counting rules need them).

### Response contract

Branch on the `error` field, **never on the status code alone** — FastAPI already uses 422 for
request-validation failures, and two distinct 409s exist.

| Outcome | Status | Body |
|---|---|---|
| Created | 201 | `BookingRead` |
| Cancelled | 200 | `BookingRead` with `status: "cancelled"` |
| Rule denial | 422 | `{"error": "rule_denied", "message": ...}` |
| Overlap conflict | 409 | `{"error": "overlap", "message": ...}` |
| Cancel unknown id | 404 | `{"error": "not_found", "message": ...}` |
| Cancel already-cancelled | 409 | `{"error": "already_cancelled", "message": ...}` |
| Malformed request | 422 | FastAPI `detail`, **no `error` key** |

Cancel a booking with `curl -X DELETE http://localhost:8000/bookings/1`.

## Running the E2E suite

```bash
cd app/e2e
npm test
```

> ### Stop your dev servers first
>
> `playwright.config.ts` sets `reuseExistingServer: false` for **both** the backend and the frontend,
> and boots its own pair against a throwaway database in `app/e2e/.tmp/`. If your own servers are
> running, the suite refuses to start:
>
> ```
> Error: http://localhost:8000 is already used, make sure that nothing is running
> on the port/url or set reuseExistingServer:true in config.webServer.
> ```
>
> This is deliberate, not a bug. Reusing a developer's backend would let the suite create and cancel
> bookings in the real `./skej.db`. Stop both servers and re-run.

Other scripts: `npm run test:headed` to watch it drive a real browser, `npm run report` to open the
HTML report. The suite is serialised to one worker — a single backend process owning a single SQLite
file cannot be tested in parallel.

## Changing the slot configuration

`app/frontend/src/config.ts` is the single source of truth for the grid:

```ts
export const calendarConfig: CalendarConfig = {
  slotMinutes: 30,
  openHour: 6,
  closeHour: 23,
}
```

Changing `slotMinutes` from 30 to 10 must require **no other edit** — if a component hardcodes a slot
count or row height, that is a bug in the component.

Two constraints:

1. **`slotMinutes` must divide the availability window evenly.** `assertConfigIsCoherent` throws
   otherwise (e.g. 45 minutes across a 17-hour window), because a partial trailing slot would silently
   hide bookable time.
2. **`openHour` / `closeHour` must be kept in sync with the backend.** They mirror
   `AVAILABILITY_OPEN` / `AVAILABILITY_CLOSE` in `app/backend/app/rules_stub.py`, and **the backend is
   authoritative** — it re-evaluates every booking against its own values. Widening the window only in
   the frontend produces a grid offering slots the server will refuse.

Stream 3 replaces these compile-time constants with per-Space rules fetched at runtime.

## Running the CI gates locally

`.github/workflows/ci.yml` runs four jobs. Each maps to commands you can run before pushing.

**Frontend:**

```bash
cd app/frontend
npm run lint && npm test && npm run build
```

**Backend:**

```bash
cd app/backend
./venv/bin/black --check . && ./venv/bin/flake8 . && ./venv/bin/pytest
```

Locally this reports **71 passed, 29 skipped**. The skips are Stream 2's Postgres-backed identity and
migration tests, which need a live database. CI supplies one as a service container; to run them
locally, start Postgres and export the URL CI uses:

```bash
DATABASE_URL=postgresql+psycopg://skej:skej@localhost:5432/skej ./venv/bin/pytest
```

Note the two variables are distinct: Stream 1's SQLite tests read `SKEJ_DATABASE_URL`, Stream 2's
Alembic tests read `DATABASE_URL`.

**Rule engine:**

```bash
cd rules
./venv/bin/black --check . && ./venv/bin/flake8 . && ./venv/bin/pytest
```

**E2E:** as above, with no dev servers running.

```bash
cd app/e2e && npm test
```

## Current limitations

These are known and intentional for the current phase — not bugs to file.

- **Authentication is stubbed.** There is a single hardcoded default user and a single default space
  (`app/backend/app/db/constants.py`). Every request is that user; there is no login, no ownership
  check, and any booking can be cancelled by anyone. Stream 2 replaces this with Auth0 and
  multi-tenant Spaces.

- **The rule engine is a stub.** `app/backend/app/rules_stub.py` hardcodes four rules (duration,
  availability hours, no-past, 60-day horizon) as plain functions. The real engine is Stream 3, and
  its interface has already drifted from the stub's — `RuleResult(pass, fail_reason)` versus
  `RuleResult(allowed, message)`, and a split context model. The behaviour matches (both fail fast on
  the first denial); only the shape differs, and the adapter lives in one place
  (`app/backend/app/routers/bookings.py`).

- **Timezone mismatch between grid and rules.** The calendar builds slot times in the **browser's
  local timezone** and serialises them with `toISOString()`, while the backend's availability rule
  compares the **UTC** wall clock against `AVAILABILITY_OPEN` / `AVAILABILITY_CLOSE`. Under a non-zero
  UTC offset these disagree: a 06:30 slot the grid renders as bookable can come back `rule_denied`.
  The Playwright suite pins the browser to `timezoneId: 'UTC'` to stay deterministic, which sidesteps
  the issue rather than fixing it (see the comment in `app/e2e/playwright.config.ts`). Resolving this
  is a product decision deferred to the Stream 3 integration.

- **Viewing past bookings is out of scope** this phase, for users and admins alike. Calendar
  navigation is bounded to the current week through 60 days ahead.

See [`DEFERRED.md`](DEFERRED.md) for features explicitly out of scope, and the `stream-*-plan.md`
files for per-stream task breakdowns.
