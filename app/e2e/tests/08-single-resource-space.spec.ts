/**
 * Test 8 — a Space with exactly one active Resource opens it (task 5.7).
 *
 * Both of `app.sandbox_seed`'s ordinary Spaces (A and B) are seeded with two
 * Resources each — deliberately, so the Space-wide frequency cap and the
 * multi-court picker are both demonstrable — so neither exercises this task's
 * redirect. The seeded **archived Space** does: `create_space` auto-creates
 * exactly one Resource and nothing in the seed adds a second or renames it,
 * so it is a genuine single-Resource Space already sitting in the sandbox.
 * Its owner-only membership and archived status are irrelevant here — reads
 * (`preview`, `list_resources`) work on an archived Space exactly as they do
 * on a live one; only writes are refused (`service.SpaceArchivedError`) — so
 * this spec adds no seed changes, matching the task's instruction not to grow
 * the shared seed for one test.
 */

import type { APIRequestContext } from '@playwright/test'

import {
  BACKEND_URL,
  expect,
  mintSandboxToken,
  SANDBOX_OWNER_SUB,
  signInAsSandbox,
  test,
} from './fixtures'

const ARCHIVED_SPACE_NAME = 'Sandbox Archived Space'

/** Discovers the archived Space's `public_id` and its one Resource through the API. */
async function findArchivedSpaceResource(
  api: APIRequestContext,
): Promise<{ publicId: string; resourceId: number }> {
  const token = await mintSandboxToken(api, SANDBOX_OWNER_SUB)
  const headers = { Authorization: `Bearer ${token}` }

  // `GET /spaces` excludes archived Spaces by default (`include_archived`
  // defaults false), and this Space is archived by construction.
  const spacesResponse = await api.get(`${BACKEND_URL}/spaces?include_archived=true`, { headers })
  expect(spacesResponse.ok(), `GET /spaces failed: ${spacesResponse.status()}`).toBeTruthy()
  const spaces = (await spacesResponse.json()) as { public_id: string; name: string }[]
  const archived = spaces.find((space) => space.name === ARCHIVED_SPACE_NAME)
  expect(archived, `${ARCHIVED_SPACE_NAME} not found in the seeded data`).toBeTruthy()

  const resourcesResponse = await api.get(
    `${BACKEND_URL}/spaces/${archived!.public_id}/resources`,
    { headers },
  )
  expect(resourcesResponse.ok(), `GET .../resources failed: ${resourcesResponse.status()}`).toBeTruthy()
  const resources = (await resourcesResponse.json()) as { id: number }[]
  expect(resources.length, `${ARCHIVED_SPACE_NAME} does not have exactly one Resource`).toBe(1)

  return { publicId: archived!.public_id, resourceId: resources[0].id }
}

test.describe('a Space with exactly one active Resource', () => {
  test('opens straight onto its calendar rather than a one-item picker', async ({ page, api }) => {
    const { publicId, resourceId } = await findArchivedSpaceResource(api)

    await signInAsSandbox(page, SANDBOX_OWNER_SUB)
    await page.goto(`/s/${publicId}`)

    // The redirect, not a render-through: the URL itself has to end up on the
    // Resource, or a refresh/bookmark/share of this screen would land
    // somewhere else than the calendar the visitor is looking at.
    await expect(page).toHaveURL(`/s/${publicId}/resources/${resourceId}`)
    await expect(page.getByTestId('calendar-grid')).toBeVisible()
    await expect(page.getByTestId('resource-list')).toHaveCount(0)

    // The back link would only bounce to a picker that immediately redirects
    // forward again — hidden here for the same reason it is asserted absent
    // in `ResourceCalendarPage.test.tsx`.
    await expect(page.getByTestId('resource-calendar-back')).toHaveCount(0)
  })
})
