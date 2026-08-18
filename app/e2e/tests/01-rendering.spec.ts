/**
 * Test 1 — UI rendering.
 *
 * The calendar loads and the grid it draws matches the shape the Space
 * actually holds.
 *
 * The expected count of offered starts comes from `fixtures.ts`'s
 * `SLOTS_PER_DAY`, which mirrors `sandbox_seed.py`'s `SPACE_A_SHAPE` rather
 * than any frontend constant — there is no client-side slot size left to
 * import. What a date offers is the server's answer (`GET
 * /spaces/{public_id}/calendar`, `.claude/rules/calendar-shape.md`), so the
 * seed's own document is the only honest source for what this grid must draw.
 */

import {
  expect,
  gotoNextWeek,
  gotoResourceCalendar,
  renderedDateKeys,
  slotId,
  SLOTS_PER_DAY,
  test,
} from './fixtures'
import { formatMinutesLabel } from '../../frontend/src/config'
import { DAYS_PER_WEEK } from '../../frontend/src/calendar/week'

test('the calendar renders a grid matching the shape the Space holds', async ({ page }) => {
  await gotoResourceCalendar(page)

  await expect(page.getByTestId('calendar')).toBeVisible()
  const grid = page.getByTestId('calendar-grid')
  await expect(grid).toBeVisible()

  await expect(page.locator('[data-testid^="calendar-day-"]')).toHaveCount(DAYS_PER_WEEK)

  const dateKeys = await renderedDateKeys(page)
  const firstDay = dateKeys[0]

  // Each day column publishes how many starts it was offered; assert it agrees
  // with the shape the sandbox seed wrote.
  await expect(page.getByTestId(`calendar-column-${firstDay}`)).toHaveAttribute(
    'data-offered-starts',
    String(SLOTS_PER_DAY),
  )

  // And assert the starts were actually drawn, not just counted in an
  // attribute.
  await expect(page.getByTestId(slotId(firstDay, 0))).toBeVisible()
  await expect(page.getByTestId(slotId(firstDay, SLOTS_PER_DAY - 1))).toBeVisible()
  // Local midnight is the day boundary: a start at or past 1440 is never
  // drawn, whatever the shape represents (`ops/done/stream-7/passed-midnight.md`).
  await expect(page.getByTestId(`slot-${firstDay}-1440`)).toHaveCount(0)

  // The first label is midnight, because Space A's own shape opens there. The
  // grid still renders the whole day whatever a Space offers — closed time is
  // painted rather than clipped away, so a booking made before an admin
  // narrowed the shape still has canvas to sit on instead of vanishing from a
  // calendar it is genuinely still on.
  await expect(page.getByTestId(slotId(firstDay, 0))).toHaveAttribute(
    'aria-label',
    `${firstDay} ${formatMinutesLabel(0)}`,
  )
  expect(formatMinutesLabel(0)).toBe('00:00')
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
