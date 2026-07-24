/**
 * Shared fixtures and helpers for the Stream 1 E2E suite.
 *
 * ## How each test gets a clean database
 *
 * Every test runs against the same backend process and the same SQLite file
 * (the config boots one of each), so isolation has to come from resetting state
 * rather than from separate databases. Three options were on the table:
 *
 * 1. **A fresh file per worker** — doesn't help. There is one worker by design,
 *    and tests inside a worker would still share its file.
 * 2. **Restarting the backend between tests** — genuinely clean, but Playwright's
 *    `webServer` boots once per run, so this would mean managing uvicorn by hand
 *    and paying a process spawn per test.
 * 3. **Cancelling every booking through the public API** — what this does.
 *
 * Option 3 wins because it is *sufficient* here, not merely convenient. Cancels
 * are soft deletes, so rows survive — but nothing Stream 1 exposes can see them:
 * `GET /bookings` excludes cancelled rows by default, and the driver's overlap
 * check considers only `status = 'confirmed'`, so a cancelled booking frees its
 * interval for rebooking. The observable state a test can reach is therefore
 * identical to an empty database. It also needs no test-only endpoint on the
 * backend, which is the real cost of the alternatives: a `POST /test/reset`
 * would be production surface area existing solely for this suite.
 *
 * The one thing it does not give is a clean *row count*, which matters to
 * exactly one future consumer — Stream 3's history-counting rules. When those
 * land and a test needs "this user has made zero bookings ever", this reset
 * stops being sufficient and the suite should move to option 2.
 */

import { test as base, expect, type APIRequestContext, type Page } from '@playwright/test'

import { slotStartMinutes, slotsPerDay } from '../../frontend/src/config'
import { slotTestId } from '../../frontend/src/calendar/week'

const BACKEND_URL = 'http://localhost:8000'

/**
 * `src/auth/sandboxToken.ts`'s `SANDBOX_SUB_STORAGE_KEY`, mirrored as a
 * literal rather than imported. Unlike `slotStartMinutes` and `slotTestId`
 * above, that module sits behind the `auth` barrel, which pulls in the api
 * client and its `import.meta.env` reference — meaningful under Vite, not
 * under the plain esbuild transform Playwright loads this file with. The two
 * copies must change together if the storage key ever does.
 */
const SANDBOX_SUB_STORAGE_KEY = 'skej.sandbox.sub'

/** A window wide enough to sweep up anything a test could have created. */
const SWEEP_YEARS = 1

export interface Booking {
  id: number
  resource_id: number
  user_id: number
  start_at: string
  end_at: string
  status: 'confirmed' | 'cancelled'
  created_at: string
  cancelled_at: string | null
}

/** Every confirmed booking the backend currently holds. */
export async function listAllBookings(api: APIRequestContext): Promise<Booking[]> {
  const now = Date.now()
  const year = SWEEP_YEARS * 365 * 24 * 60 * 60 * 1000
  const response = await api.get(`${BACKEND_URL}/bookings`, {
    params: {
      from: new Date(now - year).toISOString(),
      to: new Date(now + year).toISOString(),
    },
  })
  expect(response.ok(), `GET /bookings failed: ${response.status()}`).toBeTruthy()
  return (await response.json()) as Booking[]
}

/**
 * Creates a booking directly against the API, bypassing the UI.
 *
 * For tests whose subject is what happens *given* an existing booking. Driving
 * the booking flow to set those up would make them fail for reasons that belong
 * to test 2, and make the failure message point at the wrong thing.
 */
export async function createBookingViaApi(
  api: APIRequestContext,
  startAt: Date,
  endAt: Date,
): Promise<Booking> {
  const response = await api.post(`${BACKEND_URL}/bookings`, {
    data: { start_at: startAt.toISOString(), end_at: endAt.toISOString() },
  })
  expect(
    response.status(),
    `POST /bookings failed: ${response.status()} ${await response.text()}`,
  ).toBe(201)
  return (await response.json()) as Booking
}

/** Cancels every confirmed booking, returning the calendar to "nothing booked". */
async function resetBookings(api: APIRequestContext): Promise<void> {
  for (const booking of await listAllBookings(api)) {
    const response = await api.delete(`${BACKEND_URL}/bookings/${booking.id}`)
    expect(
      response.ok(),
      `DELETE /bookings/${booking.id} failed: ${response.status()}`,
    ).toBeTruthy()
  }
}

export const test = base.extend<{ api: APIRequestContext }>({
  api: async ({ playwright }, use) => {
    // A bare request context rather than `request` from the page's fixture: this
    // talks to the backend directly, so it must not inherit `baseURL` (the
    // frontend) or any browser state.
    const context = await playwright.request.newContext()
    await resetBookings(context)
    await use(context)
    await context.dispose()
  },
})

export { expect }

/**
 * The seeded identities `app.sandbox_seed` (task 4.8) plants, mirrored here as
 * literals rather than imported: that module is Python and this is
 * TypeScript, so the two are mirrors of one another, not one source of truth
 * — `src/auth/sandboxToken.ts`'s `DEFAULT_SANDBOX_SUB` is the third mirror.
 * All three must change together if the seed's subs ever do.
 */
export const SANDBOX_OWNER_SUB = 'sandbox|owner'
export const SANDBOX_OWNER_EMAIL = 'owner@sandbox.open-skej.local'

export const SANDBOX_ADMIN_SUB = 'sandbox|admin'
export const SANDBOX_ADMIN_EMAIL = 'admin@sandbox.open-skej.local'

export const SANDBOX_MEMBER_SUB = 'sandbox|member'
export const SANDBOX_MEMBER_EMAIL = 'member@sandbox.open-skej.local'

export const SANDBOX_STRANGER_SUB = 'sandbox|stranger'
export const SANDBOX_STRANGER_EMAIL = 'stranger@sandbox.open-skej.local'

/**
 * Mints a sandbox-signed access token for `sub`, the way `SandboxAuthProvider`
 * does on the frontend.
 *
 * For a spec that needs a bearer token directly against the API — a resource-
 * scoped booking route, say — rather than through a signed-in page. Reaches
 * `POST /sandbox/token` (`app/backend/app/routers/sandbox.py`), which exists
 * only because `playwright.config.ts` starts the backend with
 * `SANDBOX_AUTH=true`; against a normally configured backend this route is a
 * genuine 404, not a 403 — see `.claude/rules/identity-and-access.md`.
 */
export async function mintSandboxToken(api: APIRequestContext, sub: string): Promise<string> {
  const response = await api.post(`${BACKEND_URL}/sandbox/token`, { data: { sub } })
  expect(response.ok(), `POST /sandbox/token failed: ${response.status()}`).toBeTruthy()
  const body = (await response.json()) as { access_token: string }
  return body.access_token
}

/**
 * Pins the sandbox identity a page authenticates as, before its first
 * navigation.
 *
 * `SandboxAuthProvider` (`src/auth/sandboxToken.ts`) reads the sub to mint a
 * token for out of `localStorage`, so it has to be there *before* the app's
 * own script runs — setting it after `page.goto` would race the app's first
 * read and land the wrong identity or none at all. `addInitScript` runs
 * before every script on every subsequent navigation in this `page`, which is
 * what makes this reliable ahead of a `goto` the caller has not made yet.
 *
 * Defaults to the seeded owner, the same default `SandboxAuthProvider` falls
 * back to when nothing has chosen a sub — a spec that never calls this at all
 * still authenticates as somebody, exactly as unmodified `page.goto('/')` did
 * before this task.
 */
export async function signInAsSandbox(page: Page, sub: string = SANDBOX_OWNER_SUB): Promise<void> {
  await page.addInitScript(
    ({ key, value }: { key: string; value: string }) => window.localStorage.setItem(key, value),
    { key: SANDBOX_SUB_STORAGE_KEY, value: sub },
  )
}

/**
 * The `YYYY-MM-DD` keys of the seven days currently rendered.
 *
 * Read from the DOM rather than computed in Node on purpose. The browser runs
 * with `timezoneId: 'UTC'` while the Node test process inherits the developer's
 * own timezone, so a `new Date()` here and one in the page can disagree about
 * what today is — and that disagreement would show up as a test that passes in
 * CI and fails in Tel Aviv. Asking the page which days it drew removes the
 * question.
 */
export async function renderedDateKeys(page: Page): Promise<string[]> {
  const headers = page.locator('[data-testid^="calendar-day-"]')
  await expect(headers).toHaveCount(7)
  const ids = await headers.evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute('data-testid') ?? ''),
  )
  return ids.map((id) => id.replace('calendar-day-', ''))
}

/**
 * A `Date` that round-trips through `toDateKey` back to `key`.
 *
 * Built with the local-time constructor because `toDateKey` reads local
 * components; parsing `key` as ISO (which `Date.parse` treats as UTC) would
 * shift the day by one under a negative offset.
 */
export function dateFromKey(key: string): Date {
  const [year, month, day] = key.split('-').map(Number)
  return new Date(year, month - 1, day)
}

/** The `data-testid` of slot `index` on the day identified by `key`. */
export function slotId(key: string, index: number): string {
  return slotTestId(dateFromKey(key), index)
}

/**
 * The instant slot `index` on `key` begins, as the backend will record it.
 *
 * Valid only because the browser is pinned to UTC (see `playwright.config.ts`):
 * the grid builds slot times in *local* time, which under that pin is UTC.
 */
export function slotInstant(key: string, index: number): Date {
  const [year, month, day] = key.split('-').map(Number)
  const minutes = slotStartMinutes(index)
  return new Date(
    Date.UTC(year, month - 1, day, Math.floor(minutes / 60), minutes % 60, 0, 0),
  )
}

/**
 * Loads the app and pages forward one week, returning that week's date keys.
 *
 * Every booking test needs slots that are unambiguously in the future: the
 * backend denies anything starting before `now`, and within the *current* week
 * which slots are still bookable depends on the time of day the suite runs —
 * run it at 23:30 and the whole of today is refused. The next week is entirely
 * future and entirely inside the 60-day horizon, whenever the suite runs.
 */
export async function gotoNextWeek(page: Page): Promise<string[]> {
  await page.goto('/')
  await expect(page.getByTestId('calendar-grid')).toBeVisible()

  const next = page.getByTestId('calendar-next-week')
  await expect(next).toBeEnabled()
  await next.click()

  // The grid re-fetches on navigation and disables every slot until the new
  // week's bookings are known, so waiting for the loader to clear is what makes
  // the first click land on an enabled button.
  await expect(page.getByTestId('calendar-loading')).toHaveCount(0)

  return renderedDateKeys(page)
}

/** Slot count per day, derived from the frontend config rather than hardcoded. */
export { slotsPerDay }
