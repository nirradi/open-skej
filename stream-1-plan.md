# Stream 1: Core E2E Booking — Implementation Plan

## Context

Open-Skej books time on shared resources (a tennis court, expensive equipment). Stream 1 builds the
full-stack happy/sad path: a calendar UI, a booking submission flow, real persistence, and a friendly
denial message when a booking is refused. It deliberately stubs the two things other streams own —
authentication (Stream 2) and the real rule engine (Stream 3) — so the end-to-end flow can be proven
before those land.

Current state of the repo: the scaffold is bare. `app/frontend` is an untouched Vite + React 19 + TS
template (no Tailwind, no calendar library). `app/backend/app/main.py` is a hello-world FastAPI app
with no data layer. `rules/rules/` is an empty package. CI (`.github/workflows/ci.yml`) already runs
lint + build for the frontend and `black --check`, `flake8`, `pytest` for both Python packages — every
task below must keep CI green.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Overlap prevention | Hard integrity invariant, enforced in the data layer | A shared resource cannot be double-booked. This is not a business rule, so it must not depend on the rule engine being correct. |
| Overlap on SQLite | `BEGIN IMMEDIATE` txn wrapping overlap-check + insert | SQLite has no exclusion constraint, but it serializes writes, so this is race-free rather than best-effort. |
| Overlap on Postgres | Deferred to Stream 2 via the driver interface | Postgres does this declaratively: `EXCLUDE USING gist (resource_id WITH =, tstzrange(start_at, end_at) WITH &&)`. Handles variable-length bookings natively. |
| Calendar UI | Custom React + Tailwind time-slot grid | Avoids React 19 / Vite 8 peer-dependency risk from `react-big-calendar`, and owning the render loop makes slot size and availability hours trivially config-driven. |
| Slot config | 30 min slots, 06:00–23:00, **config-driven** | Defaults only. Must be changeable to e.g. 10 min by editing one config value, with no other code changes. |
| Time storage | UTC in the DB, rendered in the browser's local timezone | Standard, and avoids ambiguity when Stream 2 adds multi-tenant spaces. |
| Cancellation | Soft delete via a `status` column, not a row delete | Stream 3's real rules count booking history ("no more than twice a week"), so cancelled bookings must remain queryable. A hard delete would destroy data the rule engine needs. |
| E2E tests | Standalone Playwright suite in `app/e2e/` | Per `.claude/rules/stream-1-booking.md`. Kept out of `app/frontend` so it can drive the real backend, not a mocked one. |

Bookings are **variable length** — the grid's slot size governs selection granularity, not the
duration a booking is allowed to be. The overlap predicate is the half-open interval test
`existing.start_at < new.end_at AND new.start_at < existing.end_at`, which handles mixed durations
without special-casing.

## Task Breakdown

Each task is one PR, delegated to a headless Sonnet sub-agent and reviewed before merge.

- [x] **1.1 — Backend data layer.** _(DONE — PR #2)_ Add SQLAlchemy to `app/backend/requirements.txt`. Define the
  `Booking` model (`id`, `resource_id`, `user_id`, `start_at`, `end_at`, `status`, `created_at`,
  `cancelled_at`; UTC-aware). `status` is `confirmed | cancelled`.
  Create `app/backend/app/db/` with a `BookingDriver` protocol and a `SQLiteBookingDriver`
  implementation exposing `list_bookings(range)`, `create_booking(...)` and `cancel_booking(id)`.
  `create_booking` must run inside `BEGIN IMMEDIATE` and raise a distinct `OverlapError` when the
  half-open predicate matches.
  **The overlap check must consider only `status = 'confirmed'` rows** — a cancelled booking must free
  its slot for rebooking. A single default user and default space are hardcoded constants for now
  (auth is Stream 2).
  Unit tests must cover the overlap edges: exact match, partial front/back, full containment, the
  adjacent-but-not-overlapping case (`prev.end_at == next.start_at` must be **allowed**), and
  rebooking a slot whose prior booking was cancelled.

- [x] **1.2 — Stub rule engine + schemas.** _(DONE — PR #3)_ Pydantic request/response schemas for bookings. A
  `app/backend/app/rules_stub.py` exposing `evaluate(booking) -> RuleResult(allowed: bool, message: str)`,
  shaped to match the real interface in `.claude/rules/stream-3-rules.md` so Stream 3 can drop in.
  Stub logic: deny bookings longer than 2 hours, and deny bookings outside availability hours, each
  with a human-readable message. Unit tested.

- [ ] **1.3 — Booking endpoints.** `GET /bookings?from=&to=` and `POST /bookings`. POST routes through
  the stub rule engine *before* touching the driver; a rule denial returns **422** with the friendly
  message, an `OverlapError` returns **409** with a distinct message. Enable CORS for the Vite dev
  origin. `TestClient` tests for success, rule denial, and overlap conflict.

- [ ] **1.4 — Cancel endpoint.** `DELETE /bookings/{id}` calling `cancel_booking`, returning 404 for an
  unknown id and 409 for an already-cancelled booking. `GET /bookings` gains an `include_cancelled`
  flag defaulting to `false`, so the calendar sees only live bookings by default. Cancellation does
  **not** route through the rule engine. `TestClient` tests including cancel-then-rebook-same-slot.

- [ ] **1.5 — Frontend foundation.** Add Tailwind to `app/frontend`. Create `src/config.ts` holding
  slot size and availability hours (the single place to change granularity). Add a typed `src/api/`
  client for the two endpoints, including a discriminated result type so 422 and 409 are distinguishable
  from a network failure. Keep `npm run lint` and `npm run build` green.

- [ ] **1.6 — Calendar grid.** A week-view time-slot grid driven entirely by `src/config.ts`, rendering
  existing bookings from `GET /bookings` and supporting click-and-drag across contiguous slots to select
  a variable-length range. No booking submission yet. Add stable `data-testid` hooks for slots and
  bookings — task 1.9 depends on them.

- [ ] **1.7 — Booking flow + states.** Wire selection to a confirm action calling `POST /bookings`.
  Render a success state and optimistic calendar update; render the denial message verbatim for 422
  and the conflict message for 409, visually distinct from an unexpected error. Loading and disabled
  states while in flight.

- [ ] **1.8 — Cancel UI.** Selecting an existing booking offers a cancel action with a confirmation
  step. On success the slot returns to available and is immediately rebookable without a page reload.

- [ ] **1.9 — Playwright E2E suite.** New `app/e2e/` directory with its own `package.json` and
  `playwright.config.ts`, driving the real Vite frontend against the real FastAPI backend (via
  `webServer` config booting both, with the backend pointed at a throwaway SQLite file). Per
  `.claude/rules/stream-1-booking.md`:
  - **Test 1 — UI rendering:** calendar loads and the grid matches the configured slot size.
  - **Test 2 — Happy path:** select a free slot → Book → success message, backend persisted, slot
    renders as booked.
  - **Test 3 — Sad path:** trigger the stub rule denial → the backend's friendly message is displayed
    and the slot remains available.
  - **Test 4 — Cancel:** book, cancel, confirm the slot frees up and can be rebooked.

  Each test must start from a clean DB so runs are order-independent. Add an `e2e` job to
  `.github/workflows/ci.yml` installing browsers via `npx playwright install --with-deps chromium`.

- [ ] **1.10 — Runbook.** A root `README.md` section covering how to run backend + frontend together
  locally, seed sample data, run the E2E suite, and change the slot configuration.

## Verification

- **Per task:** CI must pass (`npm run lint`, `npm run build`; `black --check .`, `flake8 .`, `pytest`).
- **Overlap safety (1.1):** beyond unit tests, a concurrency test firing two overlapping
  `create_booking` calls at once must result in exactly one success and one `OverlapError`.
- **Negative controls on any concurrency assertion:** "two racing calls yield one success" is
  satisfiable by a test that never actually races — two threads started together will typically see
  the winner finish before the loser issues its first statement, so the test passes even with the
  locking removed. Any such test must be validated by breaking the invariant (e.g. stripping
  `BEGIN IMMEDIATE`) and confirming it **fails**. Force the interleaving explicitly rather than
  relying on thread scheduling. Verified for 1.1: without the guard the test reports 2 successes.
- **End-to-end (after 1.8):** run backend and frontend, book an empty slot → success and it appears on
  the grid; book a 3-hour range → friendly rule denial, nothing persisted; book over an existing
  booking → 409 conflict message; cancel a booking → slot frees and rebooks cleanly; reload →
  persisted bookings reappear from SQLite.
- **Automated E2E (1.9):** `npx playwright test` in `app/e2e` passes all four specs against a live
  stack, and the same job passes in CI.
- **Config-driven check:** change slot size in `src/config.ts` from 30 to 10 and confirm the grid
  re-renders correctly with no other edits.

## Open Questions (non-blocking — collected, not blocked on)

1. How far into the future may a booking be made? (Rule scope is capped at one month per CLAUDE.md —
   does the calendar hard-stop navigation there?)
2. Should the grid show a day view / configurable number of days, or is week-only sufficient for now?
3. Are past time slots rendered as disabled, or hidden entirely?
4. Can a booking that has already started (or finished) be cancelled, or only future ones? Assumed
   **any booking is cancellable** for now, since there is no auth or ownership model yet.
