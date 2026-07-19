/**
 * Test 2 — the happy path.
 *
 * Select a free slot, book it, and confirm three separate things agree: the UI
 * says it worked, the backend actually wrote it, and the grid redraws the slot
 * as taken.
 *
 * The API check is not redundant with the success banner. `BookingPanel` sets
 * its success state from the client's `outcome`, so a bug that made every POST
 * report success would still paint the banner green. Reading the row back is
 * what distinguishes "the UI thinks it saved" from "it saved".
 */

import { expect, gotoNextWeek, listAllBookings, slotId, slotInstant, test } from './fixtures'
import { bookingTestId } from '../../frontend/src/calendar/week'
import { dragAcrossSlots } from './pointer'

/** 08:00 under the default config — comfortably inside availability hours. */
const SLOT_INDEX = 4

test('booking a free slot persists it and renders it as booked', async ({ page, api }) => {
  const [day] = await gotoNextWeek(page)

  expect(await listAllBookings(api), 'the suite must start from an empty calendar').toHaveLength(0)

  const slot = page.getByTestId(slotId(day, SLOT_INDEX))
  await expect(slot).toBeEnabled()

  await dragAcrossSlots(page, [slotId(day, SLOT_INDEX)])

  // The panel is the confirmation step; it appearing is what proves the
  // selection reached the app shell and not just the grid's local state.
  await expect(page.getByTestId('booking-panel')).toBeVisible()
  await expect(page.getByTestId('booking-duration')).toHaveText('30 minutes')

  await page.getByTestId('booking-confirm').click()

  await expect(page.getByTestId('booking-success')).toBeVisible()
  await expect(page.getByTestId('booking-success')).toHaveText(
    'Booked. Your reservation is on the calendar.',
  )

  // --- what the server actually holds ---------------------------------
  const bookings = await listAllBookings(api)
  expect(bookings).toHaveLength(1)
  const [booking] = bookings
  expect(booking.status).toBe('confirmed')
  expect(new Date(booking.start_at).getTime()).toBe(slotInstant(day, SLOT_INDEX).getTime())
  expect(new Date(booking.end_at).getTime()).toBe(slotInstant(day, SLOT_INDEX + 1).getTime())

  // --- what the grid now shows ----------------------------------------
  await expect(page.getByTestId(bookingTestId(booking.id))).toBeVisible()
  await expect(slot).toHaveAttribute('data-blocked', 'booked')
  await expect(slot).toBeDisabled()
})

test('a booking survives a page reload', async ({ page, api }) => {
  const [day] = await gotoNextWeek(page)

  await dragAcrossSlots(page, [slotId(day, SLOT_INDEX)])
  await page.getByTestId('booking-confirm').click()
  await expect(page.getByTestId('booking-success')).toBeVisible()

  const [booking] = await listAllBookings(api)

  // Reload and page forward again: the block must come back from SQLite rather
  // than from the optimistic state the previous render left behind.
  await gotoNextWeek(page)
  await expect(page.getByTestId(bookingTestId(booking.id))).toBeVisible()
  await expect(page.getByTestId(slotId(day, SLOT_INDEX))).toBeDisabled()
})
