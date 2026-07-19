/**
 * Test 3 — the sad path.
 *
 * A booking longer than the stub engine's two-hour maximum is refused, and the
 * message the engine wrote reaches the screen unaltered.
 *
 * ## Why the expected copy is asserted verbatim
 *
 * `rules_stub.py` documents `message` as "written to be shown verbatim to an end
 * user", and `BookingPanel` renders the `rule_denied` branch without a prefix or
 * a paraphrase precisely to honour that. Asserting a substring, or merely that
 * *some* alert appeared, would pass against a UI that wrapped the copy in
 * "Error: " or truncated it — the two failures this assertion exists to catch.
 * If the backend legitimately rewords the rule, this test failing is the correct
 * outcome: the copy is part of the contract.
 *
 * ## Why the slot count is computed
 *
 * `5` is only "more than two hours" while a slot is 30 minutes. Deriving the
 * count from `slotMinutes` keeps the test triggering the rule it names after a
 * config change, instead of quietly selecting 50 minutes and asserting a denial
 * that never comes.
 */

import { expect, gotoNextWeek, listAllBookings, slotId, test } from './fixtures'
import { calendarConfig } from '../../frontend/src/config'
import { formatDuration as formatDurationForUi } from '../../frontend/src/booking/summary'
import { dragAcrossSlots } from './pointer'

/** Mirrors `MAX_BOOKING_DURATION` in `app/backend/app/rules_stub.py`. */
const MAX_BOOKING_MINUTES = 120

/** The shortest selection that breaks the rule, whatever a slot is worth. */
const SLOTS_TO_EXCEED_MAX =
  Math.floor(MAX_BOOKING_MINUTES / calendarConfig.slotMinutes) + 1

const FIRST_SLOT = 4

/**
 * Mirrors `_format_duration` in `app/backend/app/rules_stub.py`.
 *
 * Deliberately separate from the frontend's `formatDuration`, which renders the
 * *same* duration differently: the backend joins with " and " ("2 hours and 30
 * minutes"), the frontend with a space ("2 hours 30 minutes"). Both appear on
 * screen together — the panel's duration row beside the denial copy — so using
 * one to assert the other silently passes only while they happen to agree.
 * (The inconsistency itself is cosmetic and noted in the plan, not fixed here.)
 */
function formatDurationForMessage(totalMinutes: number): string {
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  const parts: string[] = []
  if (hours) parts.push(hours === 1 ? '1 hour' : `${hours} hours`)
  if (minutes) parts.push(minutes === 1 ? '1 minute' : `${minutes} minutes`)
  return parts.length > 0 ? parts.join(' and ') : '0 minutes'
}

test('an over-long booking is denied with the engine\'s own message', async ({ page, api }) => {
  const [day] = await gotoNextWeek(page)

  const slotIds = Array.from({ length: SLOTS_TO_EXCEED_MAX }, (_, i) => slotId(day, FIRST_SLOT + i))
  await dragAcrossSlots(page, slotIds)

  // The drag really did select the whole range — otherwise a denial could be
  // caused by something other than duration and the test would prove nothing.
  await expect(page.getByTestId('calendar-selection')).toContainText(
    `Selected ${SLOTS_TO_EXCEED_MAX} slots`,
  )

  const selectedMinutes = SLOTS_TO_EXCEED_MAX * calendarConfig.slotMinutes
  await expect(page.getByTestId('booking-duration')).toHaveText(formatDurationForUi(selectedMinutes))

  await page.getByTestId('booking-confirm').click()

  const expectedMessage =
    `Bookings can be at most ${formatDurationForMessage(MAX_BOOKING_MINUTES)} long,` +
    ` and this one is ${formatDurationForMessage(selectedMinutes)}.` +
    ' Please shorten it and try again.'

  const denial = page.getByTestId('booking-denied')
  await expect(denial).toBeVisible()
  await expect(denial).toHaveText(expectedMessage)

  // A denial must not be dressed as a success or a conflict.
  await expect(page.getByTestId('booking-success')).toHaveCount(0)
  await expect(page.getByTestId('booking-conflict')).toHaveCount(0)

  // --- nothing was written --------------------------------------------
  expect(await listAllBookings(api)).toHaveLength(0)

  // --- and the slots are still there to be taken ------------------------
  for (const id of slotIds) {
    const slot = page.getByTestId(id)
    await expect(slot).toBeEnabled()
    await expect(slot).not.toHaveAttribute('data-blocked', 'booked')
  }
})

test('shortening the selection after a denial books successfully', async ({ page, api }) => {
  const [day] = await gotoNextWeek(page)

  const tooLong = Array.from({ length: SLOTS_TO_EXCEED_MAX }, (_, i) => slotId(day, FIRST_SLOT + i))
  await dragAcrossSlots(page, tooLong)
  await page.getByTestId('booking-confirm').click()
  await expect(page.getByTestId('booking-denied')).toBeVisible()

  // The denial leaves the selection in place so the user can fix it — that is
  // the remedy the message asks for, so it had better work.
  await dragAcrossSlots(page, [slotId(day, FIRST_SLOT)])
  await expect(page.getByTestId('booking-denied')).toHaveCount(0)

  await page.getByTestId('booking-confirm').click()
  await expect(page.getByTestId('booking-success')).toBeVisible()

  expect(await listAllBookings(api)).toHaveLength(1)
})
