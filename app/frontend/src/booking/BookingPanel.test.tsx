// @vitest-environment jsdom
/**
 * Tests for the booking confirm panel.
 *
 * The panel's job is to keep four failure modes visually and semantically
 * distinct, so most of these tests assert on *which* state rendered, not merely
 * that something did. The pairs matter most:
 *
 * - a rule denial and an overlap conflict share nothing but "we didn't book it",
 *   and only the conflict refreshes the calendar;
 * - an `invalid_request` must never leak its `detail` into the DOM, because that
 *   is Pydantic's text, not copy for a user.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock, MockInstance } from 'vitest'

import { BookingPanel } from './BookingPanel'
import * as api from '../api'
import type { Booking } from '../api'
import type { SelectedInterval } from '../calendar'

const selection: SelectedInterval = {
  start: new Date(2026, 6, 24, 8, 0),
  end: new Date(2026, 6, 24, 9, 30),
}

const created: Booking = {
  id: 1,
  resource_id: 1,
  user_id: 1,
  start_at: '2026-07-24T05:00:00Z',
  end_at: '2026-07-24T06:30:00Z',
  status: 'confirmed',
  created_at: '2026-07-20T10:00:00Z',
  cancelled_at: null,
}

let createBooking: MockInstance<typeof api.createBooking>
let onCalendarChanged: Mock<() => void>

beforeEach(() => {
  createBooking = vi.spyOn(api, 'createBooking')
  onCalendarChanged = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function renderPanel(sel: SelectedInterval | null = selection) {
  return render(<BookingPanel selection={sel} onCalendarChanged={onCalendarChanged} />)
}

function book() {
  fireEvent.click(screen.getByTestId('booking-confirm'))
}

describe('the summary shown before committing', () => {
  it('prompts for a selection when there is none', () => {
    renderPanel(null)
    expect(screen.getByTestId('booking-empty')).toBeTruthy()
    expect(screen.queryByTestId('booking-confirm')).toBeNull()
  })

  it('states the day, the time range, and the duration', () => {
    renderPanel()
    expect(screen.getByTestId('booking-time').textContent).toContain('08:00')
    expect(screen.getByTestId('booking-time').textContent).toContain('09:30')
    // Variable-length bookings: the duration is not derivable from slot size,
    // so it has to be stated rather than left for the user to work out.
    expect(screen.getByTestId('booking-duration').textContent).toBe('1 hour 30 minutes')
  })
})

describe('success', () => {
  it('confirms and asks the calendar to refresh', async () => {
    createBooking.mockResolvedValue({ outcome: 'ok', data: created })
    renderPanel()
    book()

    await waitFor(() => expect(screen.getByTestId('booking-success')).toBeTruthy())
    // Refetching is how the new booking reaches the grid without a page reload.
    expect(onCalendarChanged).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('booking-denied')).toBeNull()
    expect(screen.queryByTestId('booking-conflict')).toBeNull()
  })

  it('submits the selected interval', async () => {
    createBooking.mockResolvedValue({ outcome: 'ok', data: created })
    renderPanel()
    book()

    await waitFor(() => expect(createBooking).toHaveBeenCalledWith(selection.start, selection.end))
  })
})

describe('rule_denied', () => {
  const message = 'Bookings can be at most 2 hours long, and this one is 3 hours.'

  it("renders the rule engine's copy verbatim", async () => {
    createBooking.mockResolvedValue({ outcome: 'rule_denied', message })
    renderPanel()
    book()

    const denied = await screen.findByTestId('booking-denied')
    // Verbatim, not paraphrased and not prefixed: this is the only text that
    // tells the user what to change.
    expect(denied.textContent).toBe(message)
  })

  it('is not shown as a conflict, and does not refresh the calendar', async () => {
    createBooking.mockResolvedValue({ outcome: 'rule_denied', message })
    renderPanel()
    book()

    await screen.findByTestId('booking-denied')
    expect(screen.queryByTestId('booking-conflict')).toBeNull()
    // Nothing changed on the server, so there is nothing to refetch.
    expect(onCalendarChanged).not.toHaveBeenCalled()
  })
})

describe('overlap', () => {
  const message = 'That time has just been taken by another booking.'

  it('renders a conflict distinct from a denial', async () => {
    createBooking.mockResolvedValue({ outcome: 'overlap', message })
    renderPanel()
    book()

    const conflict = await screen.findByTestId('booking-conflict')
    expect(conflict.textContent).toBe(message)
    expect(screen.queryByTestId('booking-denied')).toBeNull()
  })

  it('refreshes the calendar, because the week on screen is stale', async () => {
    createBooking.mockResolvedValue({ outcome: 'overlap', message })
    renderPanel()
    book()

    await screen.findByTestId('booking-conflict')
    // The booking that beat us is not drawn yet; without this the user is
    // staring at a slot that still looks free.
    expect(onCalendarChanged).toHaveBeenCalledTimes(1)
  })
})

describe('invalid_request', () => {
  const detail = 'body.start_at: Input should be a valid datetime'

  it('never renders the diagnostic detail', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    createBooking.mockResolvedValue({
      outcome: 'invalid_request',
      detail,
      raw: { detail: [{ msg: 'Input should be a valid datetime' }] },
    })
    renderPanel()
    book()

    const error = await screen.findByTestId('booking-error')
    // The whole point of the `invalid_request` variant: Pydantic's text is for
    // us, not for the person trying to book a tennis court.
    expect(error.textContent).not.toContain(detail)
    expect(error.textContent).not.toContain('start_at')
    expect(document.body.textContent).not.toContain('start_at')
    expect(logged).toHaveBeenCalled()
  })

  it('is not mistaken for a rule denial', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    createBooking.mockResolvedValue({ outcome: 'invalid_request', detail, raw: null })
    renderPanel()
    book()

    await screen.findByTestId('booking-error')
    expect(screen.queryByTestId('booking-denied')).toBeNull()
    expect(onCalendarChanged).not.toHaveBeenCalled()
  })
})

describe('failed', () => {
  it('shows generic copy', async () => {
    const message = "We couldn't reach the server. Check your connection and try again."
    createBooking.mockResolvedValue({ outcome: 'failed', message })
    renderPanel()
    book()

    const error = await screen.findByTestId('booking-error')
    expect(error.textContent).toBe(message)
    expect(onCalendarChanged).not.toHaveBeenCalled()
  })
})

describe('while a request is in flight', () => {
  it('disables the confirm control and cannot be double-submitted', async () => {
    let release: (value: { outcome: 'ok'; data: Booking }) => void = () => {}
    createBooking.mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }),
    )

    renderPanel()
    const confirm = screen.getByTestId('booking-confirm') as HTMLButtonElement
    book()

    await waitFor(() => expect(confirm.disabled).toBe(true))

    // A second click while in flight would otherwise create a duplicate booking.
    fireEvent.click(confirm)
    expect(createBooking).toHaveBeenCalledTimes(1)

    release({ outcome: 'ok', data: created })
    await waitFor(() => expect(screen.getByTestId('booking-success')).toBeTruthy())
  })
})

describe('when the selection moves', () => {
  it('drops a stale result so it cannot describe the new range', async () => {
    createBooking.mockResolvedValue({ outcome: 'rule_denied', message: 'Too long.' })
    const { rerender } = renderPanel()
    book()
    await screen.findByTestId('booking-denied')

    rerender(
      <BookingPanel
        selection={{ start: new Date(2026, 6, 25, 8, 0), end: new Date(2026, 6, 25, 9, 0) }}
        onCalendarChanged={onCalendarChanged}
      />,
    )

    expect(screen.queryByTestId('booking-denied')).toBeNull()
  })
})
