/**
 * Real pointer hit-testing — the part of the suite unit tests cannot replace.
 *
 * ## The tension being tested
 *
 * Booking blocks are absolutely positioned *over* the slot buttons. Until task
 * 1.8 they carried `pointer-events-none`, which made them invisible to the
 * cursor; 1.8 removed it so a block could be clicked to cancel. That creates two
 * requirements that pull against each other:
 *
 * 1. A click on a block must reach the **block** (or cancelling is impossible).
 * 2. A drag across free slots must reach the **slots** (or booking is impossible).
 *
 * `CalendarGrid.tsx` argues these can coexist because a block only ever covers
 * slots that are already `disabled`, and its own test file asserts that
 * invariant. But the invariant is about *state*, and the failure mode is about
 * *layout* — a block mis-positioned by a rounding error, an overlay stretched by
 * `inset-x-0.5`, a z-index change. jsdom computes no layout and `fireEvent`
 * dispatches directly at a chosen target, so no Vitest test in this repo can
 * observe which element the browser would actually hand a click to.
 *
 * These tests use `page.mouse` and `locator.click()`, both of which go through
 * Chromium's real input pipeline: Playwright's actionability check asserts the
 * element it targeted is the one that receives the event, so an overlay stealing
 * the click fails the test rather than being silently absorbed.
 */

import {
  createBookingViaApi,
  expect,
  gotoNextWeek,
  listAllBookings,
  slotId,
  slotInstant,
  test,
} from './fixtures'
import { bookingTestId } from '../../frontend/src/calendar/week'
import { dragAcrossSlots } from './pointer'

/** The seeded booking occupies slots 0–1; the free slots used for drags start well clear. */
const BOOKED_SLOT = 0
const FREE_FIRST = 4
const FREE_COUNT = 5

test('clicking a booking block opens the cancel panel', async ({ page, api }) => {
  await page.goto('/')
  const [day] = await gotoNextWeek(page)

  const booking = await createBookingViaApi(
    api,
    slotInstant(day, BOOKED_SLOT),
    slotInstant(day, BOOKED_SLOT + 2),
  )
  await gotoNextWeek(page)

  const block = page.getByTestId(bookingTestId(booking.id))
  await expect(block).toBeVisible()

  // Nothing is selected yet, so the cancel panel is not even rendered.
  await expect(page.getByTestId('cancel-panel')).toHaveCount(0)

  // A real click. If the block still had `pointer-events-none`, Chromium would
  // route this to the slot button underneath and Playwright would fail the
  // actionability check rather than quietly passing.
  await block.click()

  await expect(page.getByTestId('cancel-panel')).toBeVisible()
  await expect(block).toHaveAttribute('data-selected', 'true')
  await expect(page.getByTestId('cancel-start')).toBeVisible()

  // Clicking it again puts the panel away — the same hit-test, in reverse.
  await block.click()
  await expect(page.getByTestId('cancel-panel')).toHaveCount(0)
})

test('drag-to-select across free slots still works with a booking on the grid', async ({
  page,
  api,
}) => {
  await page.goto('/')
  const [day] = await gotoNextWeek(page)

  await createBookingViaApi(api, slotInstant(day, BOOKED_SLOT), slotInstant(day, BOOKED_SLOT + 2))
  await gotoNextWeek(page)

  const slotIds = Array.from({ length: FREE_COUNT }, (_, i) => slotId(day, FREE_FIRST + i))
  await dragAcrossSlots(page, slotIds)

  // A multi-slot range produced by real mouse travel, not a synthetic event.
  await expect(page.getByTestId('calendar-selection')).toContainText(`Selected ${FREE_COUNT} slots`)

  for (const id of slotIds) {
    await expect(page.getByTestId(id)).toHaveAttribute('data-selected', 'true')
  }
  // The slot just outside the range is not swept up.
  await expect(page.getByTestId(slotId(day, FREE_FIRST + FREE_COUNT))).not.toHaveAttribute(
    'data-selected',
    'true',
  )

  // Both requirements hold at once: the drag landed on the slots, and the block
  // is still clickable afterwards.
  const [existing] = await listAllBookings(api)
  await page.getByTestId(bookingTestId(existing.id)).click()
  await expect(page.getByTestId('cancel-panel')).toBeVisible()
  // Picking a booking retracts the free-range selection — one question, one answer.
  await expect(page.getByTestId('calendar-selection')).toHaveCount(0)
})

test('a drag beginning on the slot directly below a booking selects normally', async ({
  page,
  api,
}) => {
  await page.goto('/')
  const [day] = await gotoNextWeek(page)

  // Booking covers slots 0–1, so slot 2 is the first free one and its top edge
  // touches the block's bottom edge — the pixel row most likely to be claimed
  // by an overlay that is one pixel too tall.
  await createBookingViaApi(api, slotInstant(day, 0), slotInstant(day, 2))
  await gotoNextWeek(page)

  const adjacent = [slotId(day, 2), slotId(day, 3)]
  await dragAcrossSlots(page, adjacent)

  await expect(page.getByTestId('calendar-selection')).toContainText('Selected 2 slots')
  await expect(page.getByTestId('booking-panel')).toBeVisible()
})

test('a drag cannot span the slots a booking occupies', async ({ page, api }) => {
  await page.goto('/')
  const [day] = await gotoNextWeek(page)

  // Booking sits in the middle of the intended drag, at slots 6–7.
  await createBookingViaApi(api, slotInstant(day, 6), slotInstant(day, 8))
  await gotoNextWeek(page)

  await expect(page.getByTestId(slotId(day, 6))).toBeDisabled()

  // Start below it and drag up past it. `rangeBetween` refuses a range covering
  // a blocked slot, so the selection must stop short rather than swallow the
  // booked time — which would submit a request guaranteed to 409.
  await dragAcrossSlots(page, [slotId(day, 4), slotId(day, 5), slotId(day, 6), slotId(day, 9)])

  const selection = page.getByTestId('calendar-selection')
  await expect(selection).toBeVisible()
  await expect(selection).toContainText('Selected 2 slots')
  await expect(page.getByTestId(slotId(day, 9))).not.toHaveAttribute('data-selected', 'true')
})
