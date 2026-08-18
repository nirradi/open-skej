/**
 * Test 10 — writing a rule in English, on the rules page (task 7.9).
 *
 * The stream's end-to-end claim, asserted once, in the one suite that runs
 * the real thing: an admin types a prompt, the panel polls a real background
 * job to completion, the resulting rule type is added to the Space through
 * the existing "Add a rule" flow, and a real booking is refused by it.
 *
 * `playwright.config.ts` starts the backend with `RULE_GENERATION_ENABLED`
 * and `RULE_GENERATION_CLIENT=stub` — the canned, deterministic client
 * `rules/generation/stub.py` documents existing precisely for this suite:
 * it answers in milliseconds, never varies, and never depends on a network
 * or a rate limit.
 *
 * ## Why a 90-minute booking, not "more than an hour"
 *
 * The stub's generated rule always builds a `max_duration`-shaped constraint
 * whose *denial copy* is a fixed "Bookings can be at most 2 hours long" —
 * not derived from whatever minute value this spec configures — but the
 * *enforcement* itself does honour the configured minutes. Space A already
 * carries its own hand-written `max_duration` rule at 120 minutes
 * (`SPACE_A_MAX_DURATION_MINUTES`, `app/backend/app/sandbox_seed.py`), and a
 * generated type's priority always sorts after every hand-written type
 * (`.claude/rules/rule-engine.md`), so a booking has to clear 120 minutes
 * before this test's own 60-minute cap is the rule that actually gets to
 * answer. 90 minutes — three of Space A's 30-minute slots — is over this
 * test's cap and comfortably under Space A's, so the denial this spec
 * asserts is provably the *generated* rule's, not the pre-existing one.
 */

import type { Page } from '@playwright/test'

import {
  discoverSpaceAResource,
  expect,
  gotoNextWeek,
  listAllBookings,
  SANDBOX_ADMIN_SUB,
  signInAsSandbox,
  slotId,
  test,
} from './fixtures'
import { dragAcrossSlots } from './pointer'

const PROMPT = 'no booking may be longer than one hour'

/** The stub's own canned denial — see the module docstring for why it never varies with configuration. */
const EXPECTED_DENIAL = 'Bookings can be at most 2 hours long. Please shorten it and try again.'

const FIRST_SLOT = 4

/** Every rendered "Maximum duration (stub)" row's `data-testid`, in DOM order — this spec's own type only. */
async function stubDurationRowTestIds(page: Page): Promise<string[]> {
  const rows = page.getByTestId(/^rule-\d+$/).filter({ hasText: 'Maximum duration (stub)' })
  return rows.evaluateAll((elements) =>
    elements.map((element) => element.getAttribute('data-testid') ?? ''),
  )
}

test.describe('writing a rule in English', () => {
  test('a submitted prompt becomes an enforced rule, end to end', async ({ page, api }) => {
    const { publicId } = await discoverSpaceAResource(api)

    await signInAsSandbox(page, SANDBOX_ADMIN_SUB)
    await page.goto(`/s/${publicId}/rules`)
    await expect(page.getByTestId('rule-authoring-panel')).toBeVisible()

    await page.getByTestId('rule-authoring-prompt').fill(PROMPT)
    await page.getByTestId('rule-authoring-submit').click()

    // Generation is minutes of wall clock against a real model; the stub
    // answers in milliseconds, but the job still runs through the real
    // sandbox (a subprocess pytest run), so this waits rather than assuming
    // the first poll already has it.
    await expect(page.getByTestId('rule-authoring-succeeded')).toBeVisible({
      timeout: 30_000,
    })
    await expect(page.getByTestId('rule-authoring-generated-type')).toHaveText(
      'Maximum duration (stub)',
    )

    // The source is there, but behind a disclosure — not dumped on screen.
    await expect(page.getByTestId('rule-authoring-code')).toBeHidden()
    await page.getByTestId('rule-authoring-code-toggle').click()
    await expect(page.getByTestId('rule-authoring-code')).toContainText('class StubMaxDurationRule')

    // "Add this rule to the Space" drops the admin into the existing
    // "Add a rule" flow with the type preselected — see the module docstring
    // for why 60 minutes is this spec's own configured cap.
    const idsBeforeAdd = new Set(await stubDurationRowTestIds(page))
    await page.getByTestId('rule-authoring-add-to-space').click()

    const select = page.getByTestId('add-rule-type-select')
    const selectedLabel = await select.evaluate(
      (element: HTMLSelectElement) => element.selectedOptions[0]?.textContent,
    )
    expect(selectedLabel).toBe('Maximum duration (stub)')
    await expect(page.getByTestId('add-rule-type-description')).toHaveText(
      'Caps how long a single booking may run. Canned output of StubLLMClient.',
    )

    await page.getByTestId('add-rule-param-max_duration').fill('60')
    await page.getByTestId('add-rule-submit').click()

    await expect.poll(async () => stubDurationRowTestIds(page)).toHaveLength(idsBeforeAdd.size + 1)
    const idsAfterAdd = await stubDurationRowTestIds(page)
    const newRowTestId = idsAfterAdd.find((id) => !idsBeforeAdd.has(id))
    expect(newRowTestId, 'the newly added row was not found').toBeTruthy()

    try {
      // --- the booking a real member would attempt, and the engine's own refusal ---
      const [day] = await gotoNextWeek(page)
      const overCap = [0, 1, 2].map((offset) => slotId(day, FIRST_SLOT + offset))
      await dragAcrossSlots(page, overCap)
      // The drag really selected all three slots (90 minutes) — otherwise a
      // denial could be caused by something other than duration.
      await expect(page.getByTestId('calendar-selection')).toContainText('Selected 90 minutes')
      await page.getByTestId('booking-confirm').click()

      const denial = page.getByTestId('booking-denied')
      await expect(denial).toBeVisible()
      await expect(denial).toHaveText(EXPECTED_DENIAL)
      expect(await listAllBookings(api)).toHaveLength(0)

      // A booking at the cap, not over it, is admitted — proving this is a
      // real 60-minute enforcement and not merely "every booking refused".
      const atCap = [0, 1].map((offset) => slotId(day, FIRST_SLOT + offset))
      await dragAcrossSlots(page, atCap)
      await expect(page.getByTestId('calendar-selection')).toContainText('Selected 60 minutes')
      await page.getByTestId('booking-confirm').click()
      await expect(page.getByTestId('booking-success')).toBeVisible()
      expect(await listAllBookings(api)).toHaveLength(1)
    } finally {
      // Space A's configuration must be unchanged for whatever runs after
      // this file — the same discipline `09-space-rules.spec.ts` follows for
      // the row it creates.
      await signInAsSandbox(page, SANDBOX_ADMIN_SUB)
      await page.goto(`/s/${publicId}/rules`)
      const row = page.getByTestId(newRowTestId!)
      await row.locator('[data-testid$="-delete-start"]').click()
      await row.locator('[data-testid$="-delete-confirm-yes"]').click()
      await expect(row).toHaveCount(0)
    }
  })
})
