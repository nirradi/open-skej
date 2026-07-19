/**
 * Test 1 — UI rendering.
 *
 * The calendar loads and the grid it draws matches the configured slot size.
 *
 * The expected slot count is imported from `app/frontend/src/config.ts` rather
 * than written as `34`. The plan's promise is that changing `slotMinutes` from
 * 30 to 10 re-renders correctly with no other edit; a hardcoded 34 here would
 * turn keeping that promise into a test failure, which is precisely backwards.
 */

import { expect, renderedDateKeys, slotId, slotsPerDay, test } from './fixtures'
import { calendarConfig, formatSlotLabel } from '../../frontend/src/config'
import { DAYS_PER_WEEK } from '../../frontend/src/calendar/week'

test('the calendar renders a grid matching the configured slot size', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByTestId('calendar')).toBeVisible()
  const grid = page.getByTestId('calendar-grid')
  await expect(grid).toBeVisible()

  // The component publishes what it thinks the slot count is; assert it agrees
  // with the config the app was built from.
  await expect(grid).toHaveAttribute('data-slots-per-day', String(slotsPerDay))

  await expect(page.locator('[data-testid^="calendar-day-"]')).toHaveCount(DAYS_PER_WEEK)

  // And assert the slots were actually drawn, not just counted in an attribute.
  const dateKeys = await renderedDateKeys(page)
  const firstDay = dateKeys[0]

  await expect(page.getByTestId(slotId(firstDay, 0))).toBeVisible()
  await expect(page.getByTestId(slotId(firstDay, slotsPerDay - 1))).toBeVisible()
  // One past the last: proves the count is a real bound, not a lower bound.
  await expect(page.getByTestId(slotId(firstDay, slotsPerDay))).toHaveCount(0)

  // The first and last labels bracket the configured availability window.
  await expect(page.getByTestId(slotId(firstDay, 0))).toHaveAttribute(
    'aria-label',
    `${firstDay} ${formatSlotLabel(0)}`,
  )
  expect(formatSlotLabel(0)).toBe(`${String(calendarConfig.openHour).padStart(2, '0')}:00`)
})

test('the week label and navigation bounds reflect the booking horizon', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByTestId('calendar-week-label')).toBeVisible()
  // The current week is the earliest reachable, so paging back is refused
  // outright rather than silently doing nothing.
  await expect(page.getByTestId('calendar-prev-week')).toBeDisabled()
  await expect(page.getByTestId('calendar-next-week')).toBeEnabled()
})
