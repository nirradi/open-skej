/**
 * Playwright configuration for the Stream 1 end-to-end suite.
 *
 * This package is deliberately separate from `app/frontend`. The frontend's own
 * Vitest suite mocks `fetch` and renders into jsdom; this one drives a real
 * browser against a real uvicorn process writing to a real SQLite file. Keeping
 * Playwright out of the frontend's `package.json` is what stops the two from
 * quietly sharing config and turning into one suite with two runners.
 *
 * ## The throwaway database
 *
 * `SKEJ_DATABASE_URL` (see `app/backend/app/dependencies.py`) overrides
 * `DEFAULT_DATABASE_URL`, which points at `./skej.db` — a developer's real
 * local data. The suite must never write there, so the backend `webServer`
 * below is handed a path under `app/e2e/.tmp/`, which is gitignored.
 *
 * The file is deleted at *config load* time rather than in `globalSetup`,
 * because Playwright's ordering of `globalSetup` relative to `webServer` has
 * changed across versions and the guarantee "the backend boots against an empty
 * database" must not depend on which side of that line we land on. This module
 * is evaluated once, before either, so the deletion is unconditionally first.
 */

import { defineConfig, devices } from '@playwright/test'
import { existsSync, mkdirSync, rmSync } from 'node:fs'
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

/** Gitignored scratch space for the throwaway database. */
const TMP_DIR = join(here, '.tmp')
const DB_PATH = join(TMP_DIR, 'e2e.db')

// WAL mode (see `_configure_sqlite`) means the data lives across three files;
// leaving the sidecars behind would resurrect rows from a previous run.
for (const suffix of ['', '-wal', '-shm']) {
  rmSync(`${DB_PATH}${suffix}`, { force: true })
}

// SQLite will not create the containing directory, and a missing one surfaces
// as `unable to open database file` from deep inside SQLAlchemy on the first
// request rather than at boot — so create it here, after the wipe above.
mkdirSync(TMP_DIR, { recursive: true })

export default defineConfig({
  testDir: './tests',

  /**
   * One worker, no parallelism.
   *
   * The stack under test is a single backend process owning a single SQLite
   * file, so parallel tests would book against each other's state no matter how
   * the per-test reset is written. Serialising is the honest expression of that
   * constraint; the suite is four specs and runs in seconds.
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
      env: { SKEJ_DATABASE_URL: `sqlite+pysqlite:///${DB_PATH}` },
      /**
       * Never reuse. A backend already listening on 8000 is almost certainly a
       * developer's own `uvicorn`, pointed at the real `./skej.db` — reusing it
       * would let the suite create and cancel bookings in their data. Better to
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
      // Same reasoning as above, plus: a reused dev server might be running a
      // different `VITE_API_BASE_URL` and quietly talk to the wrong backend.
      reuseExistingServer: false,
      stdout: 'pipe',
      stderr: 'pipe',
      timeout: 120_000,
    },
  ],
})
