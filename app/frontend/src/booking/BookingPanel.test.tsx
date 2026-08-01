// @vitest-environment jsdom
/**
 * Tests for the booking confirm panel.
 *
 * The panel's job is to keep several failure modes visually and semantically
 * distinct, so most of these tests assert on *which* state rendered, not merely
 * that something did. The pairs matter most:
 *
 * - a rule denial and an overlap conflict share nothing but "we didn't book it",
 *   and only the conflict refreshes the calendar;
 * - `space_archived` is terminal — unlike `overlap`, there is no slot to try
 *   instead, so the confirm control is hidden rather than offered again;
 * - an `invalid_request` must never leak its `detail` into the DOM, because that
 *   is Pydantic's text, not copy for a user;
 * - the access floor (`unauthenticated` / `forbidden` / `not_found`) renders as
 *   generic copy, not as a rule denial.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock, MockInstance } from 'vitest'

import { BookingPanel } from './BookingPanel'
import * as api from '../api'
import type { Booking } from '../api'
import type { SelectedInterval } from '../calendar'
import { SYSTEM_TIME_ZONE } from '../timezone'

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'
const RESOURCE_ID = 3

const selection: SelectedInterval = {
  start: new Date(2026, 6, 24, 8, 0),
  end: new Date(2026, 6, 24, 9, 30),
}

const created: Booking = {
  id: 1,
  resource_id: RESOURCE_ID,
  user_id: 1,
  mine: true,
  start_at: '2026-07-24T05:00:00Z',
  end_at: '2026-07-24T06:30:00Z',
  status: 'confirmed',
  created_at: '2026-07-20T10:00:00Z',
  cancelled_at: null,
}

let createResourceBooking: MockInstance<typeof api.createResourceBooking>
let onCalendarChanged: Mock<() => void>

beforeEach(() => {
  createResourceBooking = vi.spyOn(api, 'createResourceBooking')
  onCalendarChanged = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function renderPanel(sel: SelectedInterval | null = selection) {
  return render(
    <BookingPanel
      publicId={PUBLIC_ID}
      resourceId={RESOURCE_ID}
      selection={sel}
      onCalendarChanged={onCalendarChanged}
      timeZone={SYSTEM_TIME_ZONE}
    />,
  )
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
    createResourceBooking.mockResolvedValue({ outcome: 'ok', data: created })
    renderPanel()
    book()

    await waitFor(() => expect(screen.getByTestId('booking-success')).toBeTruthy())
    // Refetching is how the new booking reaches the grid without a page reload.
    expect(onCalendarChanged).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('booking-denied')).toBeNull()
    expect(screen.queryByTestId('booking-conflict')).toBeNull()
  })

  it('submits the selected interval, scoped to the Space and Resource', async () => {
    createResourceBooking.mockResolvedValue({ outcome: 'ok', data: created })
    renderPanel()
    book()

    await waitFor(() =>
      expect(createResourceBooking).toHaveBeenCalledWith(
        PUBLIC_ID,
        RESOURCE_ID,
        selection.start,
        selection.end,
      ),
    )
  })
})

describe('rule_denied', () => {
  const message = 'Bookings can be at most 2 hours long, and this one is 3 hours.'

  it("renders the rule engine's copy verbatim", async () => {
    createResourceBooking.mockResolvedValue({ outcome: 'rule_denied', message })
    renderPanel()
    book()

    const denied = await screen.findByTestId('booking-denied')
    // Verbatim, not paraphrased and not prefixed: this is the only text that
    // tells the user what to change.
    expect(denied.textContent).toBe(message)
  })

  it('is not shown as a conflict, and does not refresh the calendar', async () => {
    createResourceBooking.mockResolvedValue({ outcome: 'rule_denied', message })
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
    createResourceBooking.mockResolvedValue({ outcome: 'overlap', message })
    renderPanel()
    book()

    const conflict = await screen.findByTestId('booking-conflict')
    expect(conflict.textContent).toBe(message)
    expect(screen.queryByTestId('booking-denied')).toBeNull()
  })

  it('refreshes the calendar, because the week on screen is stale', async () => {
    createResourceBooking.mockResolvedValue({ outcome: 'overlap', message })
    renderPanel()
    book()

    await screen.findByTestId('booking-conflict')
    // The booking that beat us is not drawn yet; without this the user is
    // staring at a slot that still looks free.
    expect(onCalendarChanged).toHaveBeenCalledTimes(1)
  })
})

describe('space_archived', () => {
  const message = 'This Space is archived and is no longer taking new bookings.'

  it('renders as terminal, distinct from overlap', async () => {
    createResourceBooking.mockResolvedValue({ outcome: 'space_archived', message })
    renderPanel()
    book()

    const archived = await screen.findByTestId('booking-archived')
    expect(archived.textContent).toBe(message)
    expect(screen.queryByTestId('booking-conflict')).toBeNull()
    expect(screen.queryByTestId('booking-denied')).toBeNull()
  })

  it('hides the confirm control rather than inviting a retry', async () => {
    createResourceBooking.mockResolvedValue({ outcome: 'space_archived', message })
    renderPanel()
    book()

    await screen.findByTestId('booking-archived')
    // Unlike `overlap`, there is no slot to try instead — every future create
    // against this Resource refuses the same way.
    expect(screen.queryByTestId('booking-confirm')).toBeNull()
  })

  it('does not refresh the calendar — nothing about the window changed', async () => {
    createResourceBooking.mockResolvedValue({ outcome: 'space_archived', message })
    renderPanel()
    book()

    await screen.findByTestId('booking-archived')
    expect(onCalendarChanged).not.toHaveBeenCalled()
  })
})

describe('the access floor', () => {
  it.each(['unauthenticated', 'forbidden', 'not_found'] as const)(
    'renders %s as generic copy, not as a rule denial',
    async (outcome) => {
      const message = 'Access refused.'
      createResourceBooking.mockResolvedValue({ outcome, message })
      renderPanel()
      book()

      const error = await screen.findByTestId('booking-error')
      expect(error.textContent).toBe(message)
      expect(screen.queryByTestId('booking-denied')).toBeNull()
      expect(onCalendarChanged).not.toHaveBeenCalled()
    },
  )
})

describe('invalid_request', () => {
  const detail = 'body.start_at: Input should be a valid datetime'

  it('never renders the diagnostic detail', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    createResourceBooking.mockResolvedValue({
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
    createResourceBooking.mockResolvedValue({ outcome: 'invalid_request', detail, raw: null })
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
    createResourceBooking.mockResolvedValue({ outcome: 'failed', message })
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
    createResourceBooking.mockReturnValue(
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
    expect(createResourceBooking).toHaveBeenCalledTimes(1)

    release({ outcome: 'ok', data: created })
    await waitFor(() => expect(screen.getByTestId('booking-success')).toBeTruthy())
  })
})

describe('when the selection moves', () => {
  it('drops a stale result so it cannot describe the new range', async () => {
    createResourceBooking.mockResolvedValue({ outcome: 'rule_denied', message: 'Too long.' })
    const { rerender } = renderPanel()
    book()
    await screen.findByTestId('booking-denied')

    rerender(
      <BookingPanel
        publicId={PUBLIC_ID}
        resourceId={RESOURCE_ID}
        selection={{ start: new Date(2026, 6, 25, 8, 0), end: new Date(2026, 6, 25, 9, 0) }}
        onCalendarChanged={onCalendarChanged}
        timeZone={SYSTEM_TIME_ZONE}
      />,
    )

    expect(screen.queryByTestId('booking-denied')).toBeNull()
  })
})
