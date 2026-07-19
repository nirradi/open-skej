// @vitest-environment jsdom
/**
 * Integration tests for the grid and the booking panel working together.
 *
 * These exist because of a bug the component-level tests structurally could not
 * catch. `BookingPanel` was tested in isolation with a mock `onCalendarChanged`,
 * so nothing actually cleared the selection. In the real app, booking clears it —
 * the slots have just become unbookable — which unmounted the summary and threw
 * the success message away with it. The booking was saved and the user was told
 * nothing.
 *
 * The lesson generalises: a callback mocked as a no-op hides whatever the real
 * callback does to its caller's props. Anything whose correctness depends on the
 * round trip between the two components belongs here rather than in either
 * component's own suite.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import * as api from './api'
import type { Booking } from './api'
import { bookingTestId, slotTestId, startOfWeek } from './calendar'

const NOW = new Date(2026, 6, 20, 9, 0)

function slotOn(dayOffset: number, index: number): HTMLElement {
  const day = new Date(startOfWeek(NOW))
  day.setDate(day.getDate() + dayOffset)
  return screen.getByTestId(slotTestId(day, index))
}

function bookingAt(dayOffset: number, startHour: number, endHour: number): Booking {
  const day = new Date(startOfWeek(NOW))
  day.setDate(day.getDate() + dayOffset)
  const start = new Date(day)
  start.setHours(startHour, 0, 0, 0)
  const end = new Date(day)
  end.setHours(endHour, 0, 0, 0)
  return {
    id: 1,
    resource_id: 'default-resource',
    user_id: 'default-user',
    start_at: start.toISOString(),
    end_at: end.toISOString(),
    status: 'confirmed',
    created_at: new Date().toISOString(),
    cancelled_at: null,
  }
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(NOW)
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

/** Selects one slot by driving the pointer events the grid actually listens for. */
function selectSlot(dayOffset: number, index: number) {
  const cell = slotOn(dayOffset, index)
  fireEvent.pointerDown(cell)
  fireEvent.pointerUp(window)
}

describe('booking end to end through the app shell', () => {
  it('keeps the success message visible after the selection is cleared', async () => {
    // The regression this file exists for.
    const created = bookingAt(2, 10, 10.5)
    vi.spyOn(api, 'listBookings')
      .mockResolvedValueOnce({ outcome: 'ok', data: [] })
      .mockResolvedValue({ outcome: 'ok', data: [created] })
    vi.spyOn(api, 'createBooking').mockResolvedValue({ outcome: 'ok', data: created })

    render(<App />)
    await screen.findByTestId('calendar')
    await waitFor(() => expect(slotOn(2, 8)).toBeTruthy())

    selectSlot(2, 8)
    await screen.findByTestId('booking-confirm')
    fireEvent.click(screen.getByTestId('booking-confirm'))

    // Booking clears the selection, so the summary goes away — but the
    // confirmation must not go away with it.
    await screen.findByTestId('booking-success')
    await waitFor(() => expect(screen.getByTestId('booking-empty')).toBeTruthy())
    expect(screen.getByTestId('booking-success')).toBeTruthy()
  })

  it('draws the new booking on the grid without a page reload', async () => {
    const created = bookingAt(2, 10, 11)
    vi.spyOn(api, 'listBookings')
      .mockResolvedValueOnce({ outcome: 'ok', data: [] })
      .mockResolvedValue({ outcome: 'ok', data: [created] })
    vi.spyOn(api, 'createBooking').mockResolvedValue({ outcome: 'ok', data: created })

    render(<App />)
    await waitFor(() => expect(slotOn(2, 8)).toBeTruthy())

    selectSlot(2, 8)
    fireEvent.click(await screen.findByTestId('booking-confirm'))

    // The refetch is what puts it on screen; nothing reloads the document.
    await waitFor(() => expect(api.listBookings).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByTestId(bookingTestId(created.id))).toBeTruthy())
  })

  it('keeps a rule denial visible with the selection intact so it can be adjusted', async () => {
    const message = 'Bookings can be at most 2 hours long, and this one is 3 hours.'
    vi.spyOn(api, 'listBookings').mockResolvedValue({ outcome: 'ok', data: [] })
    vi.spyOn(api, 'createBooking').mockResolvedValue({ outcome: 'rule_denied', message })

    render(<App />)
    await waitFor(() => expect(slotOn(2, 8)).toBeTruthy())

    selectSlot(2, 8)
    fireEvent.click(await screen.findByTestId('booking-confirm'))

    const denied = await screen.findByTestId('booking-denied')
    expect(denied.textContent).toBe(message)
    // Nothing was booked, so the range is still the user's to fix.
    expect(screen.getByTestId('booking-confirm')).toBeTruthy()
    expect(screen.queryByTestId('booking-empty')).toBeNull()
  })

  it('refetches on an overlap so the slot that beat the user becomes visible', async () => {
    const theirs = bookingAt(2, 10, 11)
    vi.spyOn(api, 'listBookings')
      .mockResolvedValueOnce({ outcome: 'ok', data: [] })
      .mockResolvedValue({ outcome: 'ok', data: [theirs] })
    vi.spyOn(api, 'createBooking').mockResolvedValue({
      outcome: 'overlap',
      message: 'That time has just been taken by another booking.',
    })

    render(<App />)
    await waitFor(() => expect(slotOn(2, 8)).toBeTruthy())

    selectSlot(2, 8)
    fireEvent.click(await screen.findByTestId('booking-confirm'))

    await screen.findByTestId('booking-conflict')
    await waitFor(() => expect(api.listBookings).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByTestId(bookingTestId(theirs.id))).toBeTruthy())
  })
})
