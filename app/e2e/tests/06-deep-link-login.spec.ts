/**
 * Test 6 — the deep link survives login and signup.
 *
 * A Space's `public_id` is the only handle to it that exists — Spaces are not
 * discoverable, and `/s/{public_id}` is the one public entry point. Since
 * login became the front door (task 4.10), that link is now behind a sign-in
 * step for every visitor, seeded or not, so the requirement this spec proves
 * is: a cold guest who opens the link, signed out, is returned to that exact
 * link after signing in — never to `/`, which would strand them with no way
 * back (there is no "browse Spaces" to recover it from).
 *
 * Two paths are covered, per the plan's explicit split:
 *
 * - **Login**: an existing seeded identity — the stranger, who already holds
 *   a pending access request against Space A (`app/backend/app/sandbox_seed.py`)
 *   and so lands on the "pending" status rather than "none", proving the
 *   preview resolved for *this* Space rather than a generic success screen.
 * - **Signup**: a brand-new sub with no `users` row at all. `get_current_user`
 *   provisions one just-in-time on the first authenticated call — here,
 *   `GET /preview` — so this exercises that path landing on the same deep
 *   link a returning user would, not a special "welcome" flow.
 *
 * `selectSandboxIdentity` (not `signInAsSandbox`) is what keeps the page
 * signed *out* on arrival: it only chooses which identity a sign-in would
 * commit to, leaving the sign-in card visible until the test clicks through
 * it — see that fixture's docstring for why the two are separate.
 */

import type { APIRequestContext } from '@playwright/test'

import {
  BACKEND_URL,
  expect,
  mintSandboxToken,
  SANDBOX_OWNER_SUB,
  SANDBOX_STRANGER_SUB,
  selectSandboxIdentity,
  test,
} from './fixtures'

const SPACE_A_NAME = 'Sandbox Space A (Berlin)'

/** A sub with no seeded `users` row — the true first-login/signup case. */
const NEWCOMER_SUB = 'sandbox|newcomer'

/** Reads Space A's `public_id` the way the plan requires: through the API, not hardcoded. */
async function findSpaceAPublicId(api: APIRequestContext): Promise<string> {
  const token = await mintSandboxToken(api, SANDBOX_OWNER_SUB)
  const response = await api.get(`${BACKEND_URL}/spaces`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(response.ok(), `GET /spaces failed: ${response.status()}`).toBeTruthy()
  const spaces = (await response.json()) as { public_id: string; name: string }[]
  const spaceA = spaces.find((space) => space.name === SPACE_A_NAME)
  expect(spaceA, `${SPACE_A_NAME} not found in the seeded data`).toBeTruthy()
  return spaceA!.public_id
}

test.describe('a deep link survives the login round trip', () => {
  test('an existing identity lands back on the Space after signing in', async ({ page, api }) => {
    const publicId = await findSpaceAPublicId(api)

    await selectSandboxIdentity(page, SANDBOX_STRANGER_SUB)
    await page.goto(`/s/${publicId}`)

    // Signed out on arrival: the sign-in card, not the Space's name.
    await expect(page.getByTestId('space-sign-in')).toBeVisible()
    await expect(page.getByText(SPACE_A_NAME)).toHaveCount(0)

    await page.getByTestId('login-email').click()

    // Landed back on the deep link, not redirected to '/'.
    await expect(page).toHaveURL(`/s/${publicId}`)

    // The stranger already holds a pending request against Space A — proof
    // the preview resolved for *this* Space, not a generic success screen.
    await expect(page.getByTestId('space-status-pending')).toBeVisible()
  })

  test('a brand-new identity is provisioned on first sign-in and lands on the same Space', async ({
    page,
    api,
  }) => {
    const publicId = await findSpaceAPublicId(api)

    await selectSandboxIdentity(page, NEWCOMER_SUB)
    await page.goto(`/s/${publicId}`)

    await expect(page.getByTestId('space-sign-in')).toBeVisible()

    await page.getByTestId('login-email').click()

    await expect(page).toHaveURL(`/s/${publicId}`)

    // No prior relationship to Space A: the just-in-time-provisioned user
    // reaches the same "ask for access" state a real newcomer would.
    await expect(page.getByTestId('space-status-none')).toBeVisible()
    await expect(page.getByTestId('request-access')).toBeVisible()
  })
})
