/**
 * Real-mouse interaction helpers.
 *
 * Everything here goes through `page.mouse`, which drives Chromium's actual
 * input pipeline: the browser decides which element is under the cursor and
 * dispatches to it. That is the entire point. The frontend's Vitest suite uses
 * `fireEvent`, which hands an event straight to a chosen target — so it can
 * confirm that `onPointerDown` does the right thing when it fires, but never
 * that it fires at all. jsdom has no layout and no hit-testing, so a grid where
 * an overlay swallows every pointer event passes those tests unchanged.
 *
 * Task 1.8 removed `pointer-events-none` from the booking blocks to make them
 * clickable. Whether that broke drag-to-select on the slots underneath is
 * exactly the class of question only a real browser can answer.
 */

import { expect, type Locator, type Page } from '@playwright/test'

/** The centre point of a locator's box, failing loudly if it has no layout. */
async function centreOf(locator: Locator): Promise<{ x: number; y: number }> {
  await expect(locator).toBeVisible()
  const box = await locator.boundingBox()
  expect(box, 'element has no bounding box — it is not laid out').not.toBeNull()
  return { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 }
}

/**
 * Drags the real mouse across a sequence of slots, in order.
 *
 * Moves through every intermediate slot rather than jumping from the first to
 * the last. `rangeBetween` would compute the same range either way, but a jump
 * would mean the test never proves the cursor can *travel* over the slots — and
 * travelling over them is what a booking block sitting in the middle would
 * interfere with.
 */
export async function dragAcrossSlots(page: Page, slotIds: string[]): Promise<void> {
  expect(slotIds.length, 'a drag needs at least one slot').toBeGreaterThan(0)

  const points = []
  for (const id of slotIds) {
    points.push(await centreOf(page.getByTestId(id)))
  }

  await page.mouse.move(points[0].x, points[0].y)
  await page.mouse.down()
  for (const point of points.slice(1)) {
    await page.mouse.move(point.x, point.y)
  }
  await page.mouse.up()
}
