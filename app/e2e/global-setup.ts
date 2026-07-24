/**
 * Global setup for the Stream 1 E2E suite: migrate the throwaway Postgres.
 *
 * The backend is Postgres-only now (Stream 4 unified the data layer), so the
 * suite can no longer boot against a fresh SQLite file. It runs against the
 * disposable database named by `DATABASE_URL` — the same convention the backend
 * test suite uses, and in CI a `postgres:16` service dedicated to the job. This
 * step brings that database up to the current schema with `alembic upgrade head`
 * before any test runs, so the `bookings` table exists when the first request
 * arrives.
 *
 * Per-test isolation is unchanged: `fixtures.ts` still cancels every confirmed
 * booking through the public API, which is sufficient because a cancelled row is
 * invisible to everything Stream 1 exposes. So this only needs to *exist* the
 * schema, not reset it — any confirmed rows a previous run left behind are
 * cancelled by the first test's fixture.
 *
 * After migrating it seeds the **default booking target**: `bookings.resource_id`
 * and `bookings.user_id` are now foreign keys, so the still-unauthenticated
 * `POST /bookings` needs a real default Resource and user to point at. The seed
 * is idempotent, so re-running against a database from a previous run is a no-op
 * rather than an error.
 *
 * It then plants the **deterministic sandbox seed** (`app.sandbox_seed`, task
 * 4.8): the owner, admin, member and stranger identities `fixtures.ts` signs
 * in as, plus the Spaces, Resources, and pending access-request/invitation
 * rows the suite (task 4.9 onward) authenticates against instead of relying
 * on the unauthenticated calendar alone. Like the booking-default seed, it is
 * idempotent — it resets its own rows before replanting them, so re-running
 * against a database from a previous run yields the same fixtures rather
 * than duplicates.
 *
 * The interpreter is resolved the same way `playwright.config.ts` resolves it:
 * the checked-out venv locally, or the `setup-python` interpreter on PATH in CI.
 */

import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const BACKEND_DIR = join(here, '..', 'backend')

const VENV_PYTHON = join(BACKEND_DIR, 'venv', 'bin', 'python')
const PYTHON = existsSync(VENV_PYTHON) ? VENV_PYTHON : 'python3'

export default function globalSetup(): void {
  const databaseUrl = process.env.DATABASE_URL
  if (!databaseUrl) {
    throw new Error(
      'DATABASE_URL is unset. The E2E suite runs the real backend against a ' +
        'disposable Postgres. Start one with `docker compose up -d` and export ' +
        'DATABASE_URL=postgresql+psycopg://skej:skej@localhost:5432/skej',
    )
  }

  execFileSync(PYTHON, ['-m', 'alembic', 'upgrade', 'head'], {
    cwd: BACKEND_DIR,
    env: { ...process.env, DATABASE_URL: databaseUrl },
    stdio: 'inherit',
  })

  execFileSync(PYTHON, ['-m', 'app.db.bootstrap'], {
    cwd: BACKEND_DIR,
    env: { ...process.env, DATABASE_URL: databaseUrl },
    stdio: 'inherit',
  })

  execFileSync(PYTHON, ['-m', 'app.sandbox_seed'], {
    cwd: BACKEND_DIR,
    env: { ...process.env, DATABASE_URL: databaseUrl },
    stdio: 'inherit',
  })
}
