/**
 * Test 12 — author a calendar shape by chat, publish it, and prove that the
 * member calendar and booking gate use the same published projection.
 *
 * The shape conversation uses the deterministic `StubShapeLLMClient` selected
 * by `playwright.config.ts`. The prompt is intentionally one of the stub's
 * documented patterns, so this test exercises the conversation, draft preview,
 * publish boundary, calendar projection, and shape gate without a live model.
 *
 * The owner reaches Shape Studio by clicking from the console. After publish,
 * the member reaches the Resource calendar by clicking from the Space list.
 * The final request deliberately bypasses the grid: the grid correctly omits
 * closed starts, while the API must independently refuse a request outside the
 * published window with the shape's member-facing copy.
 */

import {
  BACKEND_URL,
  discoverSpaceAResource,
  expect,
  renderedDateKeys,
  SANDBOX_MEMBER_SUB,
  SANDBOX_OWNER_SUB,
  signInAsSandbox,
  slotId,
  slotInstant,
  test,
} from './fixtures'

const PROMPT = 'from 10 to 12, 60 minute slots'
const SEED_OFFERED_STARTS_PER_DAY = 48
const PUBLISHED_OFFERED_STARTS_PER_DAY = 2
const OPENING_SLOT_INDEX = 20 // 10:00, with Space A's 30-minute seed step.
const OUTSIDE_SLOT_INDEX = 18 // 09:00, immediately before the published window.

const OUTSIDE_SHAPE_MESSAGE = "This isn't during a time we're open. Check the calendar for our hours."

test('shape authoring publishes the window the member calendar enforces', async ({ page, api }) => {
  const { publicId, resourceId, headers } = await discoverSpaceAResource(api)

  // --- owner: find Shape Studio from the console and author a draft -------
  await signInAsSandbox(page, SANDBOX_OWNER_SUB)
  await page.goto('/')
  await page.getByTestId('admin-link').click()
  await expect(page).toHaveURL('/admin')
  await page.getByTestId('space-picker').selectOption(publicId)
  await expect(page.getByTestId('space-admin')).toBeVisible()

  await page.getByTestId('space-shape-link').click()
  await expect(page).toHaveURL(`/s/${publicId}/shape`)
  await expect(page.getByTestId('shape-chat')).toBeVisible()

  const preview = page.getByTestId('shape-preview')
  const previewColumn = preview.locator('[data-testid^="calendar-column-"]').first()
  await expect(previewColumn).toHaveAttribute(
    'data-offered-starts',
    String(SEED_OFFERED_STARTS_PER_DAY),
  )
  const previewDateKey = (
    await preview.locator('[data-testid^="calendar-day-"]').first().getAttribute('data-testid')
  )?.replace('calendar-day-', '')
  expect(previewDateKey).toBeTruthy()

  await page.getByTestId('shape-message-input').fill(PROMPT)
  await page.getByTestId('shape-send').click()

  // The response returns a draft immediately, but the preview is a separate
  // projection request. The attribute assertion waits for that projection,
  // not merely for the assistant transcript to appear.
  await expect(page.getByTestId('shape-message-assistant-2')).toContainText(
    '10:00–12:00 every day',
  )
  await expect(previewColumn).toHaveAttribute(
    'data-offered-starts',
    String(PUBLISHED_OFFERED_STARTS_PER_DAY),
  )
  await expect(preview.getByTestId(slotId(previewDateKey!, OPENING_SLOT_INDEX))).toBeVisible()
  await expect(preview.getByTestId(slotId(previewDateKey!, OPENING_SLOT_INDEX + 4))).toHaveCount(0)

  await page.getByTestId('shape-publish').click()
  await expect(page.getByTestId('shape-success')).toHaveText(
    'Published. This is now what members can book.',
  )

  // --- member: reach the Resource calendar through the visible UI --------
  await signInAsSandbox(page, SANDBOX_MEMBER_SUB)
  await page.goto('/')
  await page.getByTestId(`space-list-item-${publicId}`).click()
  await expect(page).toHaveURL(`/s/${publicId}`)
  await expect(page.getByTestId('resource-list')).toBeVisible()
  await page.getByTestId(`resource-list-item-${resourceId}`).click()
  await expect(page).toHaveURL(`/s/${publicId}/resources/${resourceId}`)
  await expect(page.getByTestId('resource-calendar-heading')).toBeVisible()

  await expect(page.getByTestId('calendar-next-week')).toBeEnabled()
  await page.getByTestId('calendar-next-week').click()
  await expect(page.getByTestId('calendar-timezone-note')).toContainText('Europe/Berlin')
  await expect(page.locator('[data-testid^="calendar-column-"]').first()).toHaveAttribute(
    'data-offered-starts',
    String(PUBLISHED_OFFERED_STARTS_PER_DAY),
  )
  const [day] = await renderedDateKeys(page)
  const memberColumn = page.getByTestId(`calendar-column-${day}`)
  await expect(memberColumn).toHaveAttribute(
    'data-offered-starts',
    String(PUBLISHED_OFFERED_STARTS_PER_DAY),
  )
  await expect(page.getByTestId(slotId(day, OPENING_SLOT_INDEX))).toBeVisible()
  await expect(page.getByTestId(slotId(day, OPENING_SLOT_INDEX + 4))).toHaveCount(0)
  await expect(page.getByTestId(slotId(day, OUTSIDE_SLOT_INDEX))).toHaveCount(0)
  // The column remains a full-day canvas, with closed time as its shaded
  // background and only the published operating interval painted open.
  await expect(memberColumn).toHaveClass(/bg-slate-100/)

  // A single click selects the one permitted 60-minute duration produced by
  // the stub shape; no drag helper or new waiting primitive is needed.
  await page.getByTestId(slotId(day, OPENING_SLOT_INDEX)).click()
  await expect(page.getByTestId('calendar-selection')).toContainText('Selected 60 minutes')
  await page.getByTestId('booking-confirm').click()
  await expect(page.getByTestId('booking-success')).toHaveText(
    'Booked. Your reservation is on the calendar.',
  )

  // The grid omits the outside start, so ask the API directly as the member.
  // This proves the server gate agrees with the published projection rather
  // than merely trusting the UI not to construct that selection.
  const outsideStart = slotInstant(day, OUTSIDE_SLOT_INDEX)
  const outsideEnd = slotInstant(day, OUTSIDE_SLOT_INDEX + 2)
  const outsideResponse = await api.post(
    `${BACKEND_URL}/spaces/${publicId}/resources/${resourceId}/bookings`,
    {
      headers,
      data: { start_at: outsideStart.toISOString(), end_at: outsideEnd.toISOString() },
    },
  )
  expect(outsideResponse.status()).toBe(422)
  const outsideBody = (await outsideResponse.json()) as { error?: string; message?: string }
  expect(outsideBody.error).toBe('rule_denied')
  expect(outsideBody.message).toBe(OUTSIDE_SHAPE_MESSAGE)
})
