/**
 * Test 9 — the Space rules page (task 6.8): creating a rule, scoping it to a
 * weekday, pausing it, and deleting it, driven end to end through the real
 * backend rather than a mock.
 *
 * `max_duration` is the rule type exercised here — one required integer
 * parameter (`rules/rules/registry.py`), the simplest real member of the
 * registry, so this spec is about the page's write path rather than any one
 * rule's own logic. `SpaceRulesPage.test.tsx` is where the genericity claim
 * ("this page never branches on rule type") is proven, against a rule type
 * invented for that test; this spec instead proves the page's writes really
 * reach the server and really persist.
 *
 * ## Space A already has a `max_duration` row of its own
 *
 * It is backfilled from `SPACE_A_MAX_DURATION_MINUTES` (task 6.6), so this
 * spec's own row is a *second* instance, not the Space's only one —
 * `max_duration` is not `is_single`, and two scoped instances (one always-on,
 * one narrowed to a weekday) is exactly the registry's own intended use of
 * `applies_to`. Both rows render the identical label ("Maximum duration"), so
 * this spec cannot pick its own row out by text alone: it records every
 * `max_duration` row's id before creating one, and the id that appears
 * afterward and was not in that set is the row this spec owns. Every
 * subsequent step scopes to that one row, and the spec deletes it as its
 * last action so Space A's configuration is unchanged for whatever runs
 * after this file.
 */

import type { Page } from '@playwright/test'

import { discoverSpaceAResource, expect, SANDBOX_ADMIN_SUB, signInAsSandbox, test } from './fixtures'

/** Every rendered `max_duration` row's `data-testid` (`rule-{id}`), in DOM order. */
async function maxDurationRowTestIds(page: Page): Promise<string[]> {
  const rows = page.getByTestId(/^rule-\d+$/).filter({ hasText: 'Maximum duration' })
  return rows.evaluateAll((elements) =>
    elements.map((element) => element.getAttribute('data-testid') ?? ''),
  )
}

test.describe('the Space rules page', () => {
  test('creates a rule, scopes it to a weekday, pauses it, and deletes it', async ({
    page,
    api,
  }) => {
    const { publicId } = await discoverSpaceAResource(api)

    // The admin identity (`ADMIN_AUTH0_SUB` in `app.sandbox_seed`) is admin of
    // Space A — enough to manage its rules, and deliberately not the owner,
    // since managing rules is an admin+ action and this proves it does not
    // require ownership.
    await signInAsSandbox(page, SANDBOX_ADMIN_SUB)
    await page.goto(`/s/${publicId}/rules`)

    await expect(page.getByTestId('add-rule-panel')).toBeVisible()
    await expect(page.getByTestId('rules-list')).toBeVisible()
    const idsBeforeCreate = new Set(await maxDurationRowTestIds(page))

    // Create: pick the type, fill its one parameter, submit.
    await page.getByTestId('add-rule-type-select').selectOption('max_duration')
    await page.getByTestId('add-rule-param-max_duration_minutes').fill('90')
    await page.getByTestId('add-rule-submit').click()

    // The new row's id is whichever `max_duration` testid appeared that was
    // not there before — see the module docstring for why text alone cannot
    // tell this spec's row apart from Space A's pre-existing one.
    await expect
      .poll(async () => maxDurationRowTestIds(page))
      .toHaveLength(idsBeforeCreate.size + 1)
    const idsAfterCreate = await maxDurationRowTestIds(page)
    const newRowTestId = idsAfterCreate.find((id) => !idsBeforeCreate.has(id))
    expect(newRowTestId, 'the newly created row was not found').toBeTruthy()

    const ruleRow = page.getByTestId(newRowTestId!)
    await expect(ruleRow).toBeVisible()
    await expect(ruleRow.locator('[data-testid$="-paused-badge"]')).toHaveCount(0)

    // Scope to Wednesday (index 2 of the Mon-first `WEEKDAY_LABELS` in
    // `AppliesToEditor.tsx`) and save.
    await ruleRow.locator('[data-testid$="-applies-mode-weekdays"]').check()
    await ruleRow.locator('[data-testid$="-applies-weekday-2"]').check()
    await ruleRow.locator('[data-testid$="-applies-save"]').click()

    // A reload proves the scope actually reached the server rather than only
    // ever having lived in this component's own state.
    await page.reload()
    const ruleRowAfterReload = page.getByTestId(newRowTestId!)
    await expect(
      ruleRowAfterReload.locator('[data-testid$="-applies-mode-weekdays"]'),
    ).toBeChecked()
    await expect(ruleRowAfterReload.locator('[data-testid$="-applies-weekday-2"]')).toBeChecked()

    // Pause it.
    await ruleRowAfterReload.locator('[data-testid$="-toggle-enabled"]').click()
    await expect(ruleRowAfterReload.locator('[data-testid$="-paused-badge"]')).toBeVisible()

    // Delete it, through the confirm step — the same two-click shape
    // `ArchiveSpacePanel` and `MembersPanel` already use elsewhere in this
    // dashboard.
    await ruleRowAfterReload.locator('[data-testid$="-delete-start"]').click()
    await ruleRowAfterReload.locator('[data-testid$="-delete-confirm-yes"]').click()
    await expect(ruleRowAfterReload).toHaveCount(0)

    // A reload proves the delete persisted too, not just the local list —
    // and that Space A's own pre-existing row is untouched. `rules-list`
    // renders empty (no rows at all) for the instant between navigation and
    // the fetch resolving, and that instant would also vacuously satisfy
    // "the deleted row is gone" and "no unexpected row survived" — so both
    // checks below poll rather than read the DOM once, the same reason
    // `idsAfterCreate` above polls for the created row to appear.
    await page.reload()
    await expect.poll(async () => maxDurationRowTestIds(page)).toEqual([...idsBeforeCreate])
    expect(await page.getByTestId(newRowTestId!).count()).toBe(0)
  })
})
