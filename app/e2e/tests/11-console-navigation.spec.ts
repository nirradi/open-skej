/**
 * Test 11 — an owner reaches every management screen by clicking only (task 9.8).
 *
 * Stream 9's OVERVIEW is explicit about what it is repairing: three
 * independent QA agents each concluded the product had no management screen,
 * when `/admin` had existed the whole time — nothing in the application
 * linked to it. A spec that visits `/admin` with `page.goto` proves the
 * screen *works*; it proves nothing about whether a person could ever find
 * it, which is the actual defect this stream closes. So this file drives
 * every step by clicking what is on screen, the way the QA personas did.
 *
 * **`page.goto` is used exactly twice in this file**: the app's entry point
 * (`/`) at the start of each walk, and — in the not-found block — the
 * deliberately wrong address that block exists to test. Every other
 * navigation is a click.
 *
 * ## Space A has two active Resources, not one
 *
 * `app.sandbox_seed` gives both of its ordinary Spaces two identical
 * Resources (`08-single-resource-space.spec.ts`'s own docstring records the
 * same fact). That puts this spec in the "more than one Resource" branch the
 * task describes: `/s/{public_id}` renders `SpaceMemberView`'s picker rather
 * than redirecting straight to a calendar, so walking through the Space page
 * exercises that page's own `admin-link` directly — it is not standing in for
 * the single-Resource redirect the way it would be for a one-Resource Space.
 * The Resource calendar's *own* `admin-link` (the entry point a single-
 * Resource venue actually depends on, since it never sees `SpaceMemberView`
 * at all) is therefore not covered by that walk and gets its own test below,
 * reached by clicking into a Resource from the picker.
 */

import { discoverSpaceAResource, expect, SANDBOX_OWNER_SUB, signInAsSandbox, test } from './fixtures'

test.describe('the console is reachable by clicking, from every entry point', () => {
  test('from the front door, through every section, to the rules page and back', async ({
    page,
    api,
  }) => {
    const { publicId } = await discoverSpaceAResource(api)

    await signInAsSandbox(page, SANDBOX_OWNER_SUB)
    await page.goto('/')

    await expect(page.getByTestId('space-list')).toBeVisible()
    await page.getByTestId('admin-link').click()
    await expect(page).toHaveURL('/admin')

    // The console can default to whichever Space sorts first for this owner
    // (`AdminPage`'s docstring); Space A is picked explicitly rather than
    // assumed, so this spec does not depend on seed ordering.
    await page.getByTestId('space-picker').selectOption(publicId)
    await expect(page.getByTestId('space-admin')).toBeVisible()

    // People — the section shown on arrival, clicked anyway so the nav
    // control itself is exercised rather than only its default state.
    await page.getByTestId('admin-nav-people').click()
    await expect(page.getByTestId('admin-nav-people')).toHaveAttribute('aria-current', 'true')
    await expect(page.getByTestId('members-panel')).toBeVisible()

    // Resources
    await page.getByTestId('admin-nav-resources').click()
    await expect(page.getByTestId('admin-nav-resources')).toHaveAttribute('aria-current', 'true')
    await expect(page.getByTestId('resources-panel')).toBeVisible()

    // Settings
    await page.getByTestId('admin-nav-settings').click()
    await expect(page.getByTestId('admin-nav-settings')).toHaveAttribute('aria-current', 'true')
    await expect(page.getByTestId('space-settings-panel')).toBeVisible()
    await expect(page.getByTestId('name-input')).toBeVisible()

    // Rules — a real navigation, not a section switch, so the URL is asserted.
    await page.getByTestId('space-rules-link').click()
    await expect(page).toHaveURL(`/s/${publicId}/rules`)
    await expect(page.getByTestId('space-rules-page')).toBeVisible()

    await page.getByTestId('rules-back-link').click()
    await expect(page).toHaveURL(`/s/${publicId}`)
  })

  test('from a Space page reached by clicking a Space in the list', async ({ page, api }) => {
    const { publicId } = await discoverSpaceAResource(api)

    await signInAsSandbox(page, SANDBOX_OWNER_SUB)
    await page.goto('/')

    await page.getByTestId(`space-list-item-${publicId}`).click()
    await expect(page).toHaveURL(`/s/${publicId}`)

    // Two active Resources, so this is the picker, not the single-Resource
    // redirect — see this file's own docstring.
    await expect(page.getByTestId('space-name')).toBeVisible()
    await expect(page.getByTestId('resource-list')).toBeVisible()

    await page.getByTestId('admin-link').click()
    await expect(page).toHaveURL('/admin')
    await expect(page.getByTestId('space-admin')).toBeVisible()
  })

  test('from a Resource calendar reached by clicking into it from the Space page', async ({
    page,
    api,
  }) => {
    const { publicId, resourceId } = await discoverSpaceAResource(api)

    await signInAsSandbox(page, SANDBOX_OWNER_SUB)
    await page.goto('/')

    await page.getByTestId(`space-list-item-${publicId}`).click()
    await page.getByTestId(`resource-list-item-${resourceId}`).click()
    await expect(page).toHaveURL(`/s/${publicId}/resources/${resourceId}`)
    await expect(page.getByTestId('resource-calendar-heading')).toBeVisible()

    // The entry point a genuinely single-Resource venue depends on, since it
    // is redirected straight here and never sees `SpaceMemberView`'s own link.
    await page.getByTestId('admin-link').click()
    await expect(page).toHaveURL('/admin')
    await expect(page.getByTestId('space-admin')).toBeVisible()
  })
})

test.describe('an unmatched Space route', () => {
  test('renders a not-found view, not a blank page, and links back to the Space', async ({
    page,
    api,
  }) => {
    const { publicId } = await discoverSpaceAResource(api)

    await signInAsSandbox(page, SANDBOX_OWNER_SUB)
    // The one address in this file typed on purpose — `/settings` is one of
    // the four guessed addresses `unknown-space-routes-render-a-blank-page.md`
    // recorded, and this block exists to prove it no longer renders blank.
    await page.goto(`/s/${publicId}/settings`)

    await expect(page.getByTestId('page-not-found')).toBeVisible()

    // The literal inverse of the bug report's own measurement: a blank page
    // has empty `innerText`, so this is the assertion that would have caught it.
    const bodyText = await page.locator('body').innerText()
    expect(bodyText.trim().length).toBeGreaterThan(0)

    await page.locator(`[data-testid="page-not-found"] a[href="/s/${publicId}"]`).click()
    await expect(page).toHaveURL(`/s/${publicId}`)
  })
})
