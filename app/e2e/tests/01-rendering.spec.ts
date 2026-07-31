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

import {
  expect,
  gotoNextWeek,
  gotoResourceCalendar,
  renderedDateKeys,
  slotId,
  slotsPerDay,
  test,
} from './fixtures'
import { formatSlotLabel } from '../../frontend/src/config'
import { DAYS_PER_WEEK } from '../../frontend/src/calendar/week'

test('the calendar renders a grid matching the configured slot size', async ({ page }) => {
  await gotoResourceCalendar(page)

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

  // The first label is midnight, whatever the Space's own hours are: task 5.9
  // made the grid render the full day and grey what falls outside opening
  // hours, rather than clipping the day to them. Clipping meant a booking made
  // before an admin narrowed the hours had no row left to sit on and vanished
  // from a calendar it was still on.
  await expect(page.getByTestId(slotId(firstDay, 0))).toHaveAttribute(
    'aria-label',
    `${firstDay} ${formatSlotLabel(0)}`,
  )
  expect(formatSlotLabel(0)).toBe('00:00')
})

test('the week label and navigation bounds reflect the booking horizon', async ({ page }) => {
  await gotoResourceCalendar(page)

  await expect(page.getByTestId('calendar-week-label')).toBeVisible()
  // The current week is the earliest reachable, so paging back is refused
  // outright rather than silently doing nothing.
  await expect(page.getByTestId('calendar-prev-week')).toBeDisabled()
  await expect(page.getByTestId('calendar-next-week')).toBeEnabled()
})

test('the week lives in the URL, and a reload lands on the same one', async ({ page }) => {
  // This is the regression the reported bug actually was: paging forward used
  // to move only in-memory state, so a refresh silently snapped back to the
  // current week regardless of what was on screen.
  const pagedDateKeys = await gotoNextWeek(page)

  const url = new URL(page.url())
  expect(url.searchParams.get('week')).toBe(pagedDateKeys[0])

  await page.reload()
  await expect(page.getByTestId('calendar-loading')).toHaveCount(0)
  await expect(page.getByTestId('calendar-error')).toHaveCount(0)
  expect(await renderedDateKeys(page)).toEqual(pagedDateKeys)
})

test('"This week" is one click back from several weeks out', async ({ page }) => {
  await gotoResourceCalendar(page)
  const currentWeekDateKeys = await renderedDateKeys(page)

  const thisWeek = page.getByTestId('calendar-this-week')
  await expect(thisWeek).toBeDisabled()

  const next = page.getByTestId('calendar-next-week')
  for (let i = 0; i < 4; i += 1) {
    await next.click()
    await expect(page.getByTestId('calendar-loading')).toHaveCount(0)
  }
  await expect(page.locator('[data-testid^="calendar-day-"]').first()).not.toHaveAttribute(
    'data-testid',
    `calendar-day-${currentWeekDateKeys[0]}`,
  )
  await expect(thisWeek).toBeEnabled()

  await thisWeek.click()
  // Wait for the grid to actually show this week before reading all seven keys.
  // `calendar-loading` is not that signal: when the week's bookings are already
  // cached it never renders at all, so `toHaveCount(0)` is satisfied
  // immediately — before React has re-rendered the day headers — and the
  // one-shot `renderedDateKeys` read below then returns the *previous* week.
  // A retrying locator assertion on the first header is the wait that means
  // what this test needs, and it is the same idiom used a few lines above.
  await expect(page.locator('[data-testid^="calendar-day-"]').first()).toHaveAttribute(
    'data-testid',
    `calendar-day-${currentWeekDateKeys[0]}`,
  )
  expect(await renderedDateKeys(page)).toEqual(currentWeekDateKeys)
  await expect(thisWeek).toBeDisabled()

  const url = new URL(page.url())
  expect(url.searchParams.get('week')).toBe(currentWeekDateKeys[0])
})
