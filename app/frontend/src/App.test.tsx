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

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import * as api from './api'
import type { Booking } from './api'
import { AuthModeContext, SessionContext, type Session } from './auth'
import { bookingTestId, slotTestId } from './calendar'

// A calendar-date carrier (see `calendar/week.ts`'s docblock), independent
// of whichever zone the host running this suite happens to be in — `slotOn`
// and `bookingAt` build every day they need from this literal, never from
// `NOW` via local getters. See `NOW`'s own comment for why that distinction
// matters now that the grid resolves through the Space's own zone.
const MONDAY = new Date(2026, 6, 20)

// A real instant: 09:00 UTC, July 20 2026. The fixture Space below is
// `timezone: 'UTC'`, and built via `Date.UTC` rather than the local `Date`
// constructor precisely because of that — a local construction would name a
// different real instant on every machine, and once the grid stopped taking
// its clock from the host and started taking it from the Space (this task),
// a `NOW` that drifted relative to UTC could land on a different calendar
// day than `MONDAY` on some hosts and break every index below. Built as UTC,
// this is unambiguously "Monday, 09:00 on the Space's own clock" everywhere.
const NOW = new Date(Date.UTC(2026, 6, 20, 9, 0))

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'
const RESOURCE_ID = 3

const AUTHENTICATED_SESSION: Session = {
  status: 'authenticated',
  login: () => {},
  logout: () => {},
}

const UNAUTHENTICATED_SESSION: Session = {
  status: 'unauthenticated',
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
 * `previewSpace` backs `SpaceAccessGate`, which now sits in front of this
 * route: it must resolve `member` before the calendar renders at all.
 */
function renderApp() {
  window.history.pushState({}, '', `/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)
  vi.spyOn(api, 'previewSpace').mockResolvedValue({
    outcome: 'ok',
    data: {
      public_id: PUBLIC_ID,
      name: 'Tennis Court',
      description: null,
      status: 'member',
    },
  })
  vi.spyOn(api, 'getSpace').mockResolvedValue({
    outcome: 'ok',
    data: {
      public_id: PUBLIC_ID,
      name: 'Tennis Court',
      description: null,
      timezone: 'UTC',
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
  // `ResourceCalendarPage` fetches `GET /spaces/{public_id}/schedule` for the
  // visible week; with no entries it falls back to the shipped default — no
  // hours restriction, the default slot size — which is what this suite's
  // assertions about the grid assume. Unmocked, this is a real `fetch` with no
  // server behind it, precisely what hung this suite before this spy was
  // added.
  vi.spyOn(api, 'getSpaceSchedule').mockResolvedValue({ outcome: 'ok', data: [] })
  return render(
    <AuthModeContext value={{ kind: 'sandbox' }}>
      <SessionContext value={AUTHENTICATED_SESSION}>
        <App />
      </SessionContext>
    </AuthModeContext>,
  )
}

function slotOn(dayOffset: number, index: number): HTMLElement {
  const day = new Date(MONDAY)
  day.setDate(day.getDate() + dayOffset)
  return screen.getByTestId(slotTestId(day, index))
}

function bookingAt(dayOffset: number, startHour: number, endHour: number): Booking {
  const day = new Date(MONDAY)
  day.setDate(day.getDate() + dayOffset)
  // Built directly in UTC, not through the environment's own local `Date`
  // setters: the fixture Space below is `timezone: 'UTC'`, and the grid now
  // resolves every slot and booking through the Space's own zone rather than
  // the environment's, so a booking fixture has to mean the same thing the
  // grid does — otherwise this suite would only pass on a machine whose own
  // zone happens to be UTC.
  const start = new Date(Date.UTC(day.getFullYear(), day.getMonth(), day.getDate(), startHour))
  const end = new Date(Date.UTC(day.getFullYear(), day.getMonth(), day.getDate(), endHour))
  return {
    id: 1,
    resource_id: 1,
    user_id: 1,
    mine: true,
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

/**
 * Waits until a slot is not merely *rendered* but actually **selectable**.
 *
 * The grid disables every slot until the week's bookings have settled, so it
 * can never be clicked into a double booking against a calendar it has not
 * loaded yet. `waitFor(() => expect(slotOn(...)).toBeTruthy())` — what these
 * tests used to do — is satisfied the instant the grid paints, which is
 * *before* that. The pointer events then land on a disabled button, nothing
 * is selected, and the failure surfaces much later as a missing
 * `booking-confirm`, which reads as a broken booking panel rather than a
 * precondition that was never met.
 *
 * That was a latent weakness these tests got away with while the calendar
 * mounted immediately. `SpaceAccessGate` now resolves membership before the
 * calendar renders at all, which delays the settle past where the old
 * precondition returned, and three tests started failing for a reason that
 * had nothing to do with what they assert.
 */
async function slotIsSelectable(dayOffset: number, index: number) {
  await waitFor(() => expect((slotOn(dayOffset, index) as HTMLButtonElement).disabled).toBe(false))
}

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
    await slotIsSelectable(2, 20)

    selectSlot(2, 20)
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
    await slotIsSelectable(2, 20)

    selectSlot(2, 20)
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
    await slotIsSelectable(2, 20)

    selectSlot(2, 20)
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
    await slotIsSelectable(2, 20)

    selectSlot(2, 20)
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
    await slotIsSelectable(2, 20)

    selectSlot(2, 20)
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
    await waitFor(() => expect((slotOn(2, 20) as HTMLButtonElement).disabled).toBe(true))

    await cancelVisibleBooking(existing)

    await screen.findByTestId('cancel-success')
    // The refetch — not a reload — is what removes it.
    await waitFor(() => expect(screen.queryByTestId(bookingTestId(existing.id))).toBeNull())
    await slotIsSelectable(2, 20)

    // And the freed time is genuinely bookable again, not merely un-greyed.
    selectSlot(2, 20)
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
    await slotIsSelectable(2, 20)
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
    expect((slotOn(2, 20) as HTMLButtonElement).disabled).toBe(true)
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

    // Index 24 is 12:00, clear of the 10:00–11:00 booking.
    fireEvent.pointerDown(slotOn(2, 24))
    fireEvent.pointerOver(slotOn(2, 26))
    fireEvent.pointerUp(window)

    const summary = await screen.findByTestId('booking-time')
    expect(summary.textContent).toContain('12:00')
    expect(summary.textContent).toContain('13:30')
    // Picking a range puts the cancel panel away.
    expect(screen.queryByTestId('cancel-panel')).toBeNull()
  })
})

/**
 * Task 9.2. `unknown-space-routes-render-a-blank-page.md`: an address under
 * `/s/{public_id}` that names none of the defined routes rendered nothing at
 * all — `document.body.textContent` was `''`. `App.tsx`'s two catch-alls
 * (`path="/s/:publicId/*"` and `path="*"`, both `NotFoundPage`) are what
 * this suite holds in place.
 *
 * This describe block renders `App` directly rather than through `renderApp`
 * above — that helper wires up the booking flow's own mocks, none of which a
 * routing test needs — with its own minimal render helper instead.
 */
describe('an unmatched route renders a not-found view, not a blank page', () => {
  /**
   * `listSpaces` and `previewSpace` back the two defined routes this suite
   * also exercises as a regression check (`/` and `/s/:publicId`) — stubbed
   * so those routes settle on their own synchronous loading state rather
   * than reaching a real, absent server.
   */
  function renderAt(path: string, session: Session = AUTHENTICATED_SESSION) {
    window.history.pushState({}, '', path)
    vi.spyOn(api, 'listSpaces').mockResolvedValue({ outcome: 'ok', data: [] })
    vi.spyOn(api, 'previewSpace').mockResolvedValue({
      outcome: 'ok',
      data: { public_id: PUBLIC_ID, name: 'Tennis Court', description: null, status: 'member' },
    })
    return render(
      <AuthModeContext value={{ kind: 'sandbox' }}>
        <SessionContext value={session}>
          <App />
        </SessionContext>
      </AuthModeContext>,
    )
  }

  it.each([
    `/s/${PUBLIC_ID}/settings`,
    `/s/${PUBLIC_ID}/members`,
    `/s/${PUBLIC_ID}/admin`,
    `/s/${PUBLIC_ID}/access-requests`,
    '/nonsense',
  ])('renders the not-found view, with a non-empty body, at %s', (path) => {
    renderAt(path)
    // The bug this closes is emptiness itself, not merely the absence of the
    // new view — assert the symptom directly.
    expect(document.body.textContent).not.toBe('')
    expect(screen.getByTestId('page-not-found')).toBeTruthy()
  })

  it('offers a way back to the Space and the console when the address names one', () => {
    renderAt(`/s/${PUBLIC_ID}/settings`)
    const card = screen.getByTestId('page-not-found')
    const spaceLink = within(card).getByRole('link', { name: 'Back to this Space' })
    expect(spaceLink.getAttribute('href')).toBe(`/s/${PUBLIC_ID}`)
    const adminLink = within(card).getByRole('link', { name: 'Open the console' })
    expect(adminLink.getAttribute('href')).toBe('/admin')
  })

  it('offers only the way home for a top-level address that names no Space', () => {
    renderAt('/nonsense')
    const card = screen.getByTestId('page-not-found')
    expect(within(card).queryByRole('link', { name: 'Back to this Space' })).toBeNull()
    expect(within(card).queryByRole('link', { name: 'Open the console' })).toBeNull()
    expect(within(card).getByRole('link', { name: 'Go to your Spaces' })).toBeTruthy()
  })

  it('still resolves the defined routes, not the catch-all', () => {
    renderAt('/')
    expect(screen.getByTestId('space-list-loading')).toBeTruthy()
    expect(screen.queryByTestId('page-not-found')).toBeNull()

    cleanup()

    renderAt(`/s/${PUBLIC_ID}`)
    expect(screen.getByTestId('space-loading')).toBeTruthy()
    expect(screen.queryByTestId('page-not-found')).toBeNull()
  })

  it('gives a signed-out visitor the not-found view, not the login notice', () => {
    renderAt(`/s/${PUBLIC_ID}/settings`, UNAUTHENTICATED_SESSION)
    expect(screen.getByTestId('page-not-found')).toBeTruthy()
    expect(screen.queryByTestId('login-controls')).toBeNull()
    expect(screen.queryByTestId('auth-required')).toBeNull()
  })
})
