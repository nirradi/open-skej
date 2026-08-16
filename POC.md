# POC: server-side calendar projection

What this branch adds, and how to run it by hand. See `app/backend/app/projection.py` for the
projection core, `app/backend/app/routers/bookable.py` for the live endpoint, and
`app/backend/scripts/projection_bench_results.md` / `app/backend/scripts/bookable_endpoint_sample.md`
for measured numbers and a real captured response.

All commands below assume this worktree's root as `cwd` unless noted, and were run for real on this
machine on 2026-08-16.

## 1. Bring up Postgres

Port 5432 was already bound by the main checkout's own `docker compose` stack (project `skej`), and
the installed Docker Compose here (v2.15.1) predates the `!override` merge tag that would let an
override file cleanly replace just the `ports:` list of the tracked `docker-compose.yml` — plain
list merge in that version *concatenates* instead of replacing, so an override still tries (and
fails) to also bind 5432. The fix: `docker-compose.local.yml`, a **standalone** compose file at the
repo root (committed alongside this file, since anyone else hitting the same port conflict needs
it too) that runs the identical Postgres on host port **5433** instead. Reuse the main checkout's
Postgres instead if you prefer; nothing here requires a second instance, port 5433 was simply the
path of least resistance.

```
docker compose -f docker-compose.local.yml up -d
```

## 2. Migrate

No virtualenv exists in this worktree. Reuse the main checkout's
(`/Users/nir.radian/nirdev/skej/app/backend/venv`), pointed at this worktree's own code via
`PYTHONPATH` — this is the one shortcut every command below takes, and it is fine for a POC: the
venv holds nothing checkout-specific, only installed packages.

```
export DATABASE_URL=postgresql+psycopg://skej:skej@localhost:5433/skej
export PYTHONPATH=$(pwd)/app/backend:$(pwd)/rules
cd app/backend
/Users/nir.radian/nirdev/skej/app/backend/venv/bin/python -m alembic upgrade head
```

## 3. Seed the sandbox

Plants two Spaces with real, different canons (`app/backend/app/sandbox_seed.py`'s own module
docstring has the full picture). Space B ("Sandbox Space B (Sydney)") is the one worth projecting
against: a real `availability_hours` row, a real `max_duration` row, a `session_length` row and a
`max_bookings_per_week` row — everything the bookable endpoint needs to visibly diverge from an
unconstrained grid. Idempotent — reruns reset rather than accumulate.

```
/Users/nir.radian/nirdev/skej/app/backend/venv/bin/python -m app.sandbox_seed
```

## 4. Start the backend, in sandbox auth mode

`SANDBOX_AUTH=true` is what makes `POST /sandbox/token` exist at all (`app/backend/app/routers/sandbox.py`)
— it mints a token for any `sub` you ask for, no hosted Auth0 login needed. **This flag must never
be set outside a sandbox** — see that router's own module docstring for the guardrails that make it
safe here (a distinct issuer/audience, mutual exclusion with real Auth0 config, opt-in only).

```
export SANDBOX_AUTH=true
/Users/nir.radian/nirdev/skej/app/backend/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8123
```

(Run this in its own terminal — it stays in the foreground. Everything below assumes it is up on
`127.0.0.1:8123`.)

## 5. Get a token and find Space B

```
TOKEN=$(curl -s -X POST http://127.0.0.1:8123/sandbox/token \
  -H "Content-Type: application/json" -d '{"sub":"sandbox|owner"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

SPACE_B=$(curl -s http://127.0.0.1:8123/spaces -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; [print(s['public_id']) for s in json.load(sys.stdin) if 'Sydney' in s['name']]")

echo "$SPACE_B"
```

`sandbox|owner` (`app/backend/app/sandbox_seed.py`'s `OWNER_AUTH0_SUB`) owns both seeded Spaces, so
it satisfies the endpoint's member-or-above gate on Space B without needing a second identity. Space
B's first Resource is always id `4` on a freshly-seeded database (id `1` is the harmless
explicit-id-1 default row `ensure_booking_defaults` plants, `2` and `3` belong to Space A) — confirm
with `GET /spaces/$SPACE_B/resources` if in doubt.

## 6. Curl the endpoint

```
curl -s "http://127.0.0.1:8123/spaces/$SPACE_B/resources/4/bookable?from=2026-08-17&to=2026-08-24" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

A real captured response, with commentary tying each denial back to the Space's own configured
rules, is in `app/backend/scripts/bookable_endpoint_sample.md`.

## Re-run the projection benchmark

One line, from the repo root:

```
PYTHONPATH=app/backend:rules /Users/nir.radian/nirdev/skej/app/backend/venv/bin/python app/backend/scripts/projection_bench.py
```

Writes `app/backend/scripts/projection_bench_results.md`.

## What is a POC shortcut here, stated plainly (would not ship as-is)

* **No virtualenv in this worktree.** Every command above borrows the main checkout's venv via
  `PYTHONPATH`. Fine for a POC; a real branch gets its own environment.
* **Postgres on a second, non-standard port (5433) via `docker-compose.local.yml`.** A one-off
  workaround for this machine's old Compose version and an already-occupied 5432, not a pattern to
  fold into the product's own `docker-compose.yml`.
* **`SANDBOX_AUTH=true` and the sandbox seed.** Never a production configuration — see step 4. A
  real deployment authenticates through Auth0 and never seeds fixture Spaces at all.
* **The response's top-level `slotMinutes` is taken from the first projected day only.** Task 1 made
  the canon (and therefore the resolved session length) genuinely per-day; the wire shape specified
  for this endpoint reports one `slotMinutes` for the whole response, not one per day. Every Space
  the sandbox seeds has a single, unscoped `session_length` row, so this never shows in practice
  here — but a Space that scoped `session_length` to particular weekdays via `applies_to` would have
  a real, unreported divergence. A shipped version moves `slotMinutes` inside each day object; see
  `app/backend/app/routers/bookable.py`'s own handler docstring.
* **`reason_code` is a hash of the denial's own user-facing text**, not a real rule identity —
  inherited as-is from `app.projection._derive_reason_code`, whose own docstring already states this
  plainly. Fine for grouping identical denials inside one response; not stable across a copy edit,
  and not a fit for anything that needs to key off it.
* **The "already booked" and "canon could not be built" denials are synthesized entirely inside
  `app.routers.bookable`** (`_ExistingBookingRule`, `_AlwaysDenyRule`) — real behavior, not faked,
  but modeled as ordinary canon rules purely as an implementation convenience for reusing
  `project_days`'s scan. A shipped version would likely want overlap and configuration-integrity
  failures reported as their own distinct response shape rather than folded into the same
  `reason_code`/`reason_text` pair a real `space_rules` denial uses.
* **The 14-day window cap (`MAX_BOOKABLE_DAYS`) is a round number**, not derived from a measured
  budget the way `app.identity.router.MAX_SCHEDULE_DAYS` (62 days) was for the O(1)-per-day
  `/schedule` endpoint. See `app/backend/app/routers/bookable.py`'s module docstring for the
  reasoning; a shipped cap would want its own benchmark run at the cap's own width, not an estimate
  extrapolated from the 7-day numbers in `projection_bench_results.md`.
