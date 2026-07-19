/**
 * Test 4 — cancel.
 *
 * Book, cancel, and confirm the slot genuinely comes back: not just that the
 * block disappeared, but that the freed interval can be booked a second time
 * without a page reload. Those are different claims — a cancel that soft-deleted
 * the row but left the overlap check counting it would pass the first and fail
 * the second, and the second is the one users care about.
 */

import { expect, gotoNextWeek, listAllBookings, slotId, slotInstant, test } from './fixtures'
import { bookingTestId } from '../../frontend/src/calendar/week'
import { dragAcrossSlots } from './pointer'

const SLOT_INDEX = 6

test('a cancelled booking frees its slot and the slot can be rebooked', async ({ page, api }) => {
  const [day] = await gotoNextWeek(page)
  const slot = page.getByTestId(slotId(day, SLOT_INDEX))

  // --- book ------------------------------------------------------------
  await dragAcrossSlots(page, [slotId(day, SLOT_INDEX)])
  await page.getByTestId('booking-confirm').click()
  await expect(page.getByTestId('booking-success')).toBeVisible()

  const [created] = await listAllBookings(api)
  const block = page.getByTestId(bookingTestId(created.id))
  await expect(block).toBeVisible()

  // --- cancel ----------------------------------------------------------
  await block.click()
  await expect(page.getByTestId('cancel-panel')).toBeVisible()

  await page.getByTestId('cancel-start').click()
  await expect(page.getByTestId('cancel-confirming')).toBeVisible()
  await page.getByTestId('cancel-confirm').click()

  await expect(page.getByTestId('cancel-success')).toBeVisible()
  await expect(page.getByTestId('cancel-success')).toHaveText('Cancelled. The slot is free again.')

  // The block is gone and the slot is live again — no reload in between.
  await expect(block).toHaveCount(0)
  await expect(slot).toBeEnabled()
  expect(await listAllBookings(api), 'the cancelled booking must not be listed').toHaveLength(0)

  // --- rebook the very same interval ------------------------------------
  await dragAcrossSlots(page, [slotId(day, SLOT_INDEX)])
  await page.getByTestId('booking-confirm').click()
  await expect(page.getByTestId('booking-success')).toBeVisible()

  const after = await listAllBookings(api)
  expect(after).toHaveLength(1)
  expect(after[0].id).not.toBe(created.id)
  expect(new Date(after[0].start_at).getTime()).toBe(slotInstant(day, SLOT_INDEX).getTime())
})

test('backing out of the confirmation leaves the booking alone', async ({ page, api }) => {
  const [day] = await gotoNextWeek(page)

  await dragAcrossSlots(page, [slotId(day, SLOT_INDEX)])
  await page.getByTestId('booking-confirm').click()
  await expect(page.getByTestId('booking-success')).toBeVisible()

  const [created] = await listAllBookings(api)
  await page.getByTestId(bookingTestId(created.id)).click()
  await page.getByTestId('cancel-start').click()
  await page.getByTestId('cancel-keep').click()

  await expect(page.getByTestId('cancel-confirming')).toHaveCount(0)
  await expect(page.getByTestId(bookingTestId(created.id))).toBeVisible()
  expect(await listAllBookings(api)).toHaveLength(1)
})
