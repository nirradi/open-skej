/**
 * Playwright configuration for the Stream 1 end-to-end suite.
 *
 * This package is deliberately separate from `app/frontend`. The frontend's own
 * Vitest suite mocks `fetch` and renders into jsdom; this one drives a real
 * browser against a real uvicorn process writing to a real Postgres. Keeping
 * Playwright out of the frontend's `package.json` is what stops the two from
 * quietly sharing config and turning into one suite with two runners.
 *
 * ## The throwaway database
 *
 * The backend is Postgres-only since Stream 4 unified the data layer, so the
 * suite runs against the disposable Postgres named by `DATABASE_URL` — the same
 * convention the backend test suite uses, and in CI a `postgres:16` service
 * dedicated to the job. `global-setup.ts` runs `alembic upgrade head` against it
 * before any test, so the schema exists on the first request; per-test isolation
 * stays with `fixtures.ts`, which cancels every confirmed booking through the
 * public API. The suite refuses to run with `DATABASE_URL` unset rather than
 * inventing a default that could point at real data.
 */

import { defineConfig, devices } from '@playwright/test'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))

/**
 * The interpreter that can actually import the backend's dependencies.
 *
 * Locally that is the checked-out virtualenv: a bare `python` may not resolve
 * at all under pyenv, and even where it does it is the wrong interpreter —
 * `uvicorn` and `fastapi` are installed in the venv, not globally. In CI there
 * is no venv, and `actions/setup-python` has already put the right interpreter
 * (with the dependencies installed into it) on PATH.
 */
const VENV_PYTHON = join(here, '..', 'backend', 'venv', 'bin', 'python')
const PYTHON = existsSync(VENV_PYTHON) ? VENV_PYTHON : 'python3'

const BACKEND_PORT = 8000
const FRONTEND_PORT = 5173

/**
 * The disposable Postgres the backend and the migration step both use. Fail
 * closed: a missing value must not silently fall back to a developer's real
 * database, so the suite refuses to start rather than guess.
 */
const DATABASE_URL = process.env.DATABASE_URL
if (!DATABASE_URL) {
  throw new Error(
    'DATABASE_URL is unset. The E2E suite runs the real backend against a ' +
      'disposable Postgres. Start one with `docker compose up -d` and export ' +
      'DATABASE_URL=postgresql+psycopg://skej:skej@localhost:5432/skej',
  )
}

export default defineConfig({
  testDir: './tests',

  // Migrate the disposable Postgres to head before anything boots. See
  // `global-setup.ts`; ordering against `webServer` does not matter because the
  // backend engine is lazy and touches the database only on the first request,
  // which no test issues until global setup has returned.
  globalSetup: './global-setup.ts',

  /**
   * One worker, no parallelism.
   *
   * The stack under test is a single backend process owning a single Postgres
   * database, so parallel tests would book against each other's state no matter
   * how the per-test reset is written. Serialising is the honest expression of
   * that constraint; the suite is four specs and runs in seconds.
   */
  fullyParallel: false,
  workers: 1,

  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',

  use: {
    baseURL: `http://localhost:${FRONTEND_PORT}`,

    /**
     * Pin the browser clock to UTC.
     *
     * The grid builds slot times in the browser's local timezone and the client
     * serialises them with `toISOString()`, while the backend's availability
     * rule compares the *UTC* wall clock against `AVAILABILITY_OPEN` /
     * `AVAILABILITY_CLOSE`. Under a non-zero offset those two disagree, so a
     * 06:30 slot the grid renders as bookable can come back `rule_denied`.
     * Pinning to UTC makes them agree and keeps this suite deterministic on any
     * developer's machine. It does **not** fix the underlying mismatch — see the
     * note in the PR; that is a product decision for the Stream 3 integration,
     * not something an E2E config should paper over silently.
     */
    timezoneId: 'UTC',

    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],

  webServer: [
    {
      command: `${PYTHON} -m uvicorn app.main:app --port ${BACKEND_PORT}`,
      cwd: join(here, '..', 'backend'),
      port: BACKEND_PORT,
      /**
       * Sandbox auth mode (task 4.7), never the real Auth0 tenant. Nothing
       * `AUTH0_*` is set alongside it — `app.auth.jwt.get_token_verifier`
       * raises at verifier construction if it ever saw both, which is exactly
       * the config this suite must never produce. Task 4.9 moved this suite
       * onto a seeded sandbox session (`global-setup.ts` runs
       * `app.sandbox_seed`) instead of depending on the unauthenticated
       * calendar, so the frontend needs a real backend to mint tokens from.
       */
      env: { DATABASE_URL, SANDBOX_AUTH: 'true' },
      /**
       * Never reuse. A backend already listening on 8000 is almost certainly a
       * developer's own `uvicorn`, pointed at their real database — reusing it
       * would let the suite create and cancel bookings in that data. Better to
       * fail with "port already in use" than to silently do that.
       */
      reuseExistingServer: false,
      stdout: 'pipe',
      stderr: 'pipe',
      timeout: 60_000,
    },
    {
      command: `npm run dev -- --port ${FRONTEND_PORT} --strictPort`,
      cwd: join(here, '..', 'frontend'),
      port: FRONTEND_PORT,
      /**
       * The frontend counterpart of the backend's sandbox mode above: no
       * `VITE_AUTH0_*` variable is set, and `VITE_SANDBOX_AUTH=true` selects
       * `SandboxAuthProvider` (see `src/auth/AuthProvider.tsx`) in place of
       * `Auth0Provider`, so the api client has a sandbox-signed token to send
       * without a hosted login page ever being involved.
       */
      env: { VITE_SANDBOX_AUTH: 'true' },
      // Same reasoning as above, plus: a reused dev server might be running a
      // different `VITE_API_BASE_URL` and quietly talk to the wrong backend.
      reuseExistingServer: false,
      stdout: 'pipe',
      stderr: 'pipe',
      timeout: 120_000,
    },
  ],
})
