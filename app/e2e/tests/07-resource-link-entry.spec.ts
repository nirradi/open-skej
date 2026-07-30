/**
 * Test 7 — a Resource link admits a stranger the same way a Space link does.
 *
 * `/s/{public_id}/resources/{resource_id}` used to sit behind
 * `ProtectedRoute`, whose copy is "You need an account to see this page".
 * That is right for a members-only screen and wrong for a link somebody was
 * handed: a forwarded Resource link was a dead end — no sign-in in place, no
 * preview, no way to ask for access. Admission is Space-level, so the Resource
 * route now mounts the same `SpaceAccessGate` `/s/{public_id}` does.
 *
 * ## What this spec can and cannot prove
 *
 * It proves the **rendered entry shapes**, in a real browser against the real
 * backend: what a signed-out visitor sees, what a signed-in non-member sees,
 * and that a member still lands on the calendar.
 *
 * It deliberately does **not** try to prove the `returnTo` contract. Sandbox
 * mode's `login()` is a synchronous state update that never leaves the page,
 * so the URL does not change across a sandbox sign-in — a test asserting "I
 * signed in at the Resource URL and I am still at the Resource URL" would pass
 * even if `returnTo` were hardcoded to `/s/{public_id}` or dropped entirely.
 * It would be asserting that a redirect which never happened did not move it.
 * That contract is pinned in `src/space/SpaceAccessGate.test.tsx`, where the
 * value handed to `login` is actually observable.
 *
 * `selectSandboxIdentity` (not `signInAsSandbox`) is what keeps the page
 * signed *out* on arrival — it chooses which identity a sign-in would commit
 * to without committing it.
 */

import {
  discoverSpaceAResource,
  expect,
  SANDBOX_MEMBER_SUB,
  SANDBOX_STRANGER_SUB,
  selectSandboxIdentity,
  signInAsSandbox,
  test,
} from './fixtures'

test.describe('a forwarded Resource link', () => {
  test('offers a signed-out visitor the way in, not a members-only dead end', async ({
    page,
    api,
  }) => {
    const { publicId, resourceId } = await discoverSpaceAResource(api)

    await selectSandboxIdentity(page, SANDBOX_STRANGER_SUB)
    await page.goto(`/s/${publicId}/resources/${resourceId}`)

    // The Space link's card, at the Resource URL.
    await expect(page.getByTestId('space-sign-in')).toBeVisible()
    // `auth-required` is `ProtectedRoute`'s members-only copy — the dead end
    // this task removed. Its absence is the regression assertion.
    await expect(page.getByTestId('auth-required')).toHaveCount(0)
  })

  test('shows a signed-in non-member the Space preview and the access request, at the Resource URL', async ({
    page,
    api,
  }) => {
    const { publicId, resourceId } = await discoverSpaceAResource(api)
    const resourcePath = `/s/${publicId}/resources/${resourceId}`

    // The seeded stranger holds a *pending* request against Space A, which is
    // what proves the preview resolved for this Space rather than rendering a
    // generic screen.
    await signInAsSandbox(page, SANDBOX_STRANGER_SUB)
    await page.goto(resourcePath)

    await expect(page.getByTestId('space-preview')).toBeVisible()
    await expect(page.getByTestId('space-status-pending')).toBeVisible()

    // Load-bearing: the URL is what makes approval land them on the Resource
    // they were sent. A redirect to `/s/{publicId}` would throw that away.
    await expect(page).toHaveURL(resourcePath)
    // And the calendar is not behind it — they are still outside.
    await expect(page.getByTestId('calendar-grid')).toHaveCount(0)
  })

  test('still lets a member straight through to the calendar', async ({ page, api }) => {
    const { publicId, resourceId } = await discoverSpaceAResource(api)

    await signInAsSandbox(page, SANDBOX_MEMBER_SUB)
    await page.goto(`/s/${publicId}/resources/${resourceId}`)

    await expect(page.getByTestId('calendar-grid')).toBeVisible()
    await expect(page.getByTestId('space-preview')).toHaveCount(0)
    await expect(page.getByTestId('space-sign-in')).toHaveCount(0)
  })
})
