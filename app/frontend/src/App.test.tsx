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
import { AuthModeContext, SessionContext, type Session } from './auth'
import { bookingTestId, slotTestId, startOfWeek } from './calendar'

const NOW = new Date(2026, 6, 20, 9, 0)

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'
const RESOURCE_ID = 3

const AUTHENTICATED_SESSION: Session = {
  status: 'authenticated',
  login: () => {},
  logout: () => {},
}

/**
 * Renders the app at the per-Resource calendar route, signed in.
 *
 * `App` no longer renders the calendar at `/` unauthenticated — this file
 * predates that change and its subject is the grid and the booking panel
 * working together, not the auth gate in front of them, so the session is
 * supplied directly rather than through a real `AuthProvider`. `pushState`
 * rather than a prop: `App` mounts its own `BrowserRouter`, which reads
 * `window.location` at creation, so this is what puts it at the right route
 * before that happens.
 *
 * `getSpace` / `listResources` back `ResourceCalendarPage`'s header context
 * only — display, not access control — so they are stubbed to resolve
 * quickly rather than left to reach a real (and here, absent) server.
 */
function renderApp() {
  window.history.pushState({}, '', `/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)
  vi.spyOn(api, 'getSpace').mockResolvedValue({
    outcome: 'ok',
    data: {
      public_id: PUBLIC_ID,
      name: 'Tennis Court',
      description: null,
      timezone: 'UTC',
      opens_at: null,
      closes_at: null,
      slot_minutes: null,
      max_duration_minutes: null,
      booking_horizon_days: null,
      max_bookings_per_week: null,
      max_bookings_per_month: null,
      created_at: '2026-07-01T00:00:00Z',
      archived_at: null,
      my_role: 'member',
    },
  })
  vi.spyOn(api, 'listResources').mockResolvedValue({
    outcome: 'ok',
    data: [
      {
        id: RESOURCE_ID,
        name: 'Court 1',
        created_at: '2026-07-01T00:00:00Z',
        archived_at: null,
      },
    ],
  })
  return render(
    <AuthModeContext value={{ kind: 'sandbox' }}>
      <SessionContext value={AUTHENTICATED_SESSION}>
        <App />
      </SessionContext>
    </AuthModeContext>,
  )
}

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
    resource_id: 1,
    user_id: 1,
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
    vi.spyOn(api, 'listResourceBookings')
      .mockResolvedValueOnce({ outcome: 'ok', data: [] })
      .mockResolvedValue({ outcome: 'ok', data: [created] })
    vi.spyOn(api, 'createResourceBooking').mockResolvedValue({ outcome: 'ok', data: created })

    renderApp()
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
    vi.spyOn(api, 'listResourceBookings')
      .mockResolvedValueOnce({ outcome: 'ok', data: [] })
      .mockResolvedValue({ outcome: 'ok', data: [created] })
    vi.spyOn(api, 'createResourceBooking').mockResolvedValue({ outcome: 'ok', data: created })

    renderApp()
    await waitFor(() => expect(slotOn(2, 8)).toBeTruthy())

    selectSlot(2, 8)
    fireEvent.click(await screen.findByTestId('booking-confirm'))

    // The refetch is what puts it on screen; nothing reloads the document.
    await waitFor(() => expect(api.listResourceBookings).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByTestId(bookingTestId(created.id))).toBeTruthy())
  })

  it('keeps a rule denial visible with the selection intact so it can be adjusted', async () => {
    const message = 'Bookings can be at most 2 hours long, and this one is 3 hours.'
    vi.spyOn(api, 'listResourceBookings').mockResolvedValue({ outcome: 'ok', data: [] })
    vi.spyOn(api, 'createResourceBooking').mockResolvedValue({ outcome: 'rule_denied', message })

    renderApp()
    await waitFor(() => expect(slotOn(2, 8)).toBeTruthy())

    selectSlot(2, 8)
    fireEvent.click(await screen.findByTestId('booking-confirm'))

    const denied = await screen.findByTestId('booking-denied')
    expect(denied.textContent).toBe(message)
    // Nothing was booked, so the range is still the user's to fix.
    expect(screen.getByTestId('booking-confirm')).toBeTruthy()
    expect(screen.queryByTestId('booking-empty')).toBeNull()
  })

  it('does not open the cancel panel for a range selection', async () => {
    vi.spyOn(api, 'listResourceBookings').mockResolvedValue({ outcome: 'ok', data: [] })
    renderApp()
    await waitFor(() => expect(slotOn(2, 8)).toBeTruthy())

    selectSlot(2, 8)
    await screen.findByTestId('booking-confirm')
    // The two panels answer different questions and must not both be asking.
    expect(screen.queryByTestId('cancel-panel')).toBeNull()
  })

  it('refetches on an overlap so the slot that beat the user becomes visible', async () => {
    const theirs = bookingAt(2, 10, 11)
    vi.spyOn(api, 'listResourceBookings')
      .mockResolvedValueOnce({ outcome: 'ok', data: [] })
      .mockResolvedValue({ outcome: 'ok', data: [theirs] })
    vi.spyOn(api, 'createResourceBooking').mockResolvedValue({
      outcome: 'overlap',
      message: 'That time has just been taken by another booking.',
    })

    renderApp()
    await waitFor(() => expect(slotOn(2, 8)).toBeTruthy())

    selectSlot(2, 8)
    fireEvent.click(await screen.findByTestId('booking-confirm'))

    await screen.findByTestId('booking-conflict')
    await waitFor(() => expect(api.listResourceBookings).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByTestId(bookingTestId(theirs.id))).toBeTruthy())
  })
})

describe('cancelling end to end through the app shell', () => {
  /**
   * Sets up a week that holds one booking and comes back empty after a refetch,
   * which is what a successful cancellation looks like from the grid's side.
   */
  function withCancellableBooking(): Booking {
    const existing = bookingAt(2, 10, 11)
    vi.spyOn(api, 'listResourceBookings')
      .mockResolvedValueOnce({ outcome: 'ok', data: [existing] })
      .mockResolvedValue({ outcome: 'ok', data: [] })
    return existing
  }

  /** Clicks the block, then walks the panel's two-step confirmation. */
  async function cancelVisibleBooking(existing: Booking) {
    fireEvent.click(await screen.findByTestId(bookingTestId(existing.id)))
    fireEvent.click(await screen.findByTestId('cancel-start'))
    fireEvent.click(screen.getByTestId('cancel-confirm'))
  }

  it('frees the slot for rebooking without a page reload', async () => {
    // The claim task 1.8 is actually making, and the one no component test can
    // reach: the block goes away *and* the slots it held become selectable.
    const existing = withCancellableBooking()
    vi.spyOn(api, 'cancelResourceBooking').mockResolvedValue({
      outcome: 'ok',
      data: { ...existing, status: 'cancelled', cancelled_at: NOW.toISOString() },
    })

    renderApp()
    // 10:00–11:00 is indices 8 and 9 from a 06:00 open at 30-minute slots.
    await waitFor(() => expect((slotOn(2, 8) as HTMLButtonElement).disabled).toBe(true))

    await cancelVisibleBooking(existing)

    await screen.findByTestId('cancel-success')
    // The refetch — not a reload — is what removes it.
    await waitFor(() => expect(screen.queryByTestId(bookingTestId(existing.id))).toBeNull())
    await waitFor(() => expect((slotOn(2, 8) as HTMLButtonElement).disabled).toBe(false))

    // And the freed time is genuinely bookable again, not merely un-greyed.
    selectSlot(2, 8)
    expect(await screen.findByTestId('booking-confirm')).toBeTruthy()
  })

  it('keeps the confirmation visible after the refresh clears the selection', async () => {
    // The `App.test.tsx` bug, in its cancel-shaped form. The refetch drops the
    // grid's selected booking, which unmounts the summary; if the panel treated
    // that as "the user moved on", the cancellation would land silently.
    const existing = withCancellableBooking()
    vi.spyOn(api, 'cancelResourceBooking').mockResolvedValue({
      outcome: 'ok',
      data: { ...existing, status: 'cancelled', cancelled_at: NOW.toISOString() },
    })

    renderApp()
    await waitFor(() => expect(screen.getByTestId(bookingTestId(existing.id))).toBeTruthy())
    await cancelVisibleBooking(existing)

    await screen.findByTestId('cancel-success')
    // The summary is gone because the booking is gone; the receipt is not.
    await waitFor(() => expect(screen.queryByTestId('cancel-start')).toBeNull())
    expect(screen.getByTestId('cancel-success')).toBeTruthy()
  })

  it('treats already_cancelled as success and still frees the slot', async () => {
    // The trap the plan calls out. A double-clicked button reaches a server that
    // has already done the work; the 409 it answers with shares a status code
    // with `overlap` and means the opposite thing.
    const existing = withCancellableBooking()
    vi.spyOn(api, 'cancelResourceBooking').mockResolvedValue({
      outcome: 'already_cancelled',
      message: 'That booking has already been cancelled.',
    })

    renderApp()
    await waitFor(() => expect(screen.getByTestId(bookingTestId(existing.id))).toBeTruthy())
    await cancelVisibleBooking(existing)

    await screen.findByTestId('cancel-success')
    expect(screen.queryByTestId('cancel-error')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    // The end state the user wanted holds, so the calendar must show it.
    await waitFor(() => expect(screen.queryByTestId(bookingTestId(existing.id))).toBeNull())
    await waitFor(() => expect((slotOn(2, 8) as HTMLButtonElement).disabled).toBe(false))
  })

  it('clears a stale block on not_found without alarming the user', async () => {
    const existing = withCancellableBooking()
    vi.spyOn(api, 'cancelResourceBooking').mockResolvedValue({
      outcome: 'not_found',
      message: 'No booking with that id.',
    })

    renderApp()
    await waitFor(() => expect(screen.getByTestId(bookingTestId(existing.id))).toBeTruthy())
    await cancelVisibleBooking(existing)

    await screen.findByTestId('cancel-notice')
    expect(screen.queryByRole('alert')).toBeNull()
    await waitFor(() => expect(screen.queryByTestId(bookingTestId(existing.id))).toBeNull())
  })

  it('leaves the booking on the grid when the cancel fails', async () => {
    const existing = withCancellableBooking()
    vi.spyOn(api, 'cancelResourceBooking').mockResolvedValue({
      outcome: 'failed',
      message: "We couldn't reach the server.",
    })

    renderApp()
    await waitFor(() => expect(screen.getByTestId(bookingTestId(existing.id))).toBeTruthy())
    await cancelVisibleBooking(existing)

    await screen.findByTestId('cancel-error')
    // Nothing was cancelled, so nothing may look cancelled — the opposite
    // mistake to the one `already_cancelled` invites.
    expect(screen.getByTestId(bookingTestId(existing.id))).toBeTruthy()
    expect((slotOn(2, 8) as HTMLButtonElement).disabled).toBe(true)
    expect(api.listResourceBookings).toHaveBeenCalledTimes(1)
  })

  it('still allows dragging a free range after a booking has been clicked', async () => {
    // Regression guard at the shell level: making blocks clickable must not
    // have cost the grid its drag-to-select.
    const existing = bookingAt(2, 10, 11)
    vi.spyOn(api, 'listResourceBookings').mockResolvedValue({ outcome: 'ok', data: [existing] })

    renderApp()
    fireEvent.click(await screen.findByTestId(bookingTestId(existing.id)))
    await screen.findByTestId('cancel-panel')

    // Index 12 is 12:00, clear of the 10:00–11:00 booking.
    fireEvent.pointerDown(slotOn(2, 12))
    fireEvent.pointerOver(slotOn(2, 14))
    fireEvent.pointerUp(window)

    const summary = await screen.findByTestId('booking-time')
    expect(summary.textContent).toContain('12:00')
    expect(summary.textContent).toContain('13:30')
    // Picking a range puts the cancel panel away.
    expect(screen.queryByTestId('cancel-panel')).toBeNull()
  })
})
