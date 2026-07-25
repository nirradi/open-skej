// @vitest-environment jsdom
/**
 * Tests for the cancel panel.
 *
 * The panel's job is to sort several outcomes into a few feelings, and the
 * pair that matters most is `ok` versus `already_cancelled`. Those share
 * nothing except that the booking ends up cancelled: one is a 200, the other
 * is a 409 carrying the same status code as `overlap`, which genuinely *is* a
 * conflict. Treating them alike is the whole point, so several tests below
 * assert on what is **absent** — no error banner, no alert role — rather than
 * merely that something rendered. `already_started` is the opposite kind of
 * pair with `already_cancelled`: both are terminal and offer no retry, but one
 * is a success and the other is not.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock, MockInstance } from 'vitest'

import { CancelPanel } from './CancelPanel'
import * as api from '../api'
import type { Booking } from '../api'

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'
const RESOURCE_ID = 3

const booking: Booking = {
  id: 42,
  resource_id: RESOURCE_ID,
  user_id: 1,
  start_at: new Date(2026, 6, 24, 8, 0).toISOString(),
  end_at: new Date(2026, 6, 24, 9, 30).toISOString(),
  status: 'confirmed',
  created_at: '2026-07-20T10:00:00Z',
  cancelled_at: null,
}

const cancelled: Booking = { ...booking, status: 'cancelled', cancelled_at: '2026-07-20T11:00:00Z' }

let cancelResourceBooking: MockInstance<typeof api.cancelResourceBooking>
let onCalendarChanged: Mock<() => void>

beforeEach(() => {
  cancelResourceBooking = vi.spyOn(api, 'cancelResourceBooking')
  onCalendarChanged = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function renderPanel(target: Booking | null = booking) {
  return render(
    <CancelPanel
      publicId={PUBLIC_ID}
      resourceId={RESOURCE_ID}
      booking={target}
      onCalendarChanged={onCalendarChanged}
    />,
  )
}

/** Walks the two-step flow: open the confirmation, then commit. */
function confirmCancel() {
  fireEvent.click(screen.getByTestId('cancel-start'))
  fireEvent.click(screen.getByTestId('cancel-confirm'))
}

describe('the panel before anything is asked of it', () => {
  it('renders nothing at all when no booking is selected', () => {
    renderPanel(null)
    expect(screen.queryByTestId('cancel-panel')).toBeNull()
  })

  it('describes the selected booking', () => {
    renderPanel()
    expect(screen.getByTestId('cancel-time').textContent).toContain('08:00')
    expect(screen.getByTestId('cancel-time').textContent).toContain('09:30')
    expect(screen.getByTestId('cancel-duration').textContent).toBe('1 hour 30 minutes')
  })
})

describe('the confirmation step', () => {
  it('does not cancel on the first click', () => {
    renderPanel()
    fireEvent.click(screen.getByTestId('cancel-start'))

    // The destructive call must be behind a second, deliberate click.
    expect(cancelResourceBooking).not.toHaveBeenCalled()
    expect(screen.getByTestId('cancel-confirming')).toBeTruthy()
  })

  it('is rendered in the page rather than delegated to window.confirm', () => {
    // A native modal would block the tab, ignore our styling, and be reachable
    // only by stubbing a global — so its absence is the assertion.
    const nativeConfirm = vi.spyOn(window, 'confirm')
    renderPanel()
    fireEvent.click(screen.getByTestId('cancel-start'))

    expect(nativeConfirm).not.toHaveBeenCalled()
    expect(screen.getByTestId('cancel-confirming')).toBeTruthy()
  })

  it('backs out without cancelling when the booking is kept', () => {
    renderPanel()
    fireEvent.click(screen.getByTestId('cancel-start'))
    fireEvent.click(screen.getByTestId('cancel-keep'))

    expect(cancelResourceBooking).not.toHaveBeenCalled()
    expect(screen.queryByTestId('cancel-confirming')).toBeNull()
    // Still selected, so the user can change their mind again.
    expect(screen.getByTestId('cancel-start')).toBeTruthy()
  })
})

describe('ok', () => {
  it('confirms and asks the calendar to refresh', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'ok', data: cancelled })
    renderPanel()
    confirmCancel()

    await screen.findByTestId('cancel-success')
    expect(cancelResourceBooking).toHaveBeenCalledWith(PUBLIC_ID, RESOURCE_ID, booking.id)
    // The refetch is what frees the slot for rebooking without a page reload.
    expect(onCalendarChanged).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('cancel-error')).toBeNull()
  })
})

describe('already_cancelled', () => {
  const message = 'That booking has already been cancelled.'

  it('is treated as success, not as a conflict', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'already_cancelled', message })
    renderPanel()
    confirmCancel()

    // The trap: this is a 409, the same status `overlap` uses. But the user's
    // own cancel already landed, so the state they wanted is the state they
    // have — nothing here is worth warning them about.
    await screen.findByTestId('cancel-success')
    expect(screen.queryByTestId('cancel-error')).toBeNull()
    expect(screen.queryByTestId('cancel-notice')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('refreshes the calendar exactly as a plain success does', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'already_cancelled', message })
    renderPanel()
    confirmCancel()

    await screen.findByTestId('cancel-success')
    // Without this the block stays drawn on a slot the server thinks is free.
    expect(onCalendarChanged).toHaveBeenCalledTimes(1)
  })

  it('is indistinguishable from ok on screen', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'ok', data: cancelled })
    const okView = renderPanel()
    confirmCancel()
    const okText = (await screen.findByTestId('cancel-success')).textContent
    okView.unmount()

    cancelResourceBooking.mockResolvedValue({ outcome: 'already_cancelled', message })
    renderPanel()
    confirmCancel()
    const alreadyText = (await screen.findByTestId('cancel-success')).textContent

    // Same end state, same copy. The distinction is ours, not the user's.
    expect(alreadyText).toBe(okText)
  })

  it("never renders the server's already-cancelled copy as a warning", async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'already_cancelled', message })
    renderPanel()
    confirmCancel()

    await screen.findByTestId('cancel-success')
    // The server's phrasing reads as a complaint. Ours reads as a receipt.
    expect(document.body.textContent).not.toContain(message)
  })
})

describe('not_found', () => {
  const message = 'No booking with that id.'

  it('refreshes so the stale block disappears', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'not_found', message })
    renderPanel()
    confirmCancel()

    await screen.findByTestId('cancel-notice')
    expect(onCalendarChanged).toHaveBeenCalledTimes(1)
  })

  it('is a neutral notice, not an alarm', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'not_found', message })
    renderPanel()
    confirmCancel()

    await screen.findByTestId('cancel-notice')
    // Nothing went wrong: the booking is gone, which is what was wanted.
    expect(screen.queryByTestId('cancel-error')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('already_started', () => {
  const message = 'This booking has already started and can no longer be cancelled.'

  it('renders a plain, terminal statement', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'already_started', message })
    renderPanel()
    confirmCancel()

    const started = await screen.findByTestId('cancel-started')
    expect(started.textContent).toBe(message)
  })

  it('hides the retry control — there is no remedy', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'already_started', message })
    renderPanel()
    confirmCancel()

    await screen.findByTestId('cancel-started')
    // Unlike `failed`, trying again refuses identically every time.
    expect(screen.queryByTestId('cancel-start')).toBeNull()
  })

  it('does not refresh the calendar — nothing changed', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'already_started', message })
    renderPanel()
    confirmCancel()

    await screen.findByTestId('cancel-started')
    expect(onCalendarChanged).not.toHaveBeenCalled()
  })

  it('is not mistaken for already_cancelled', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'already_started', message })
    renderPanel()
    confirmCancel()

    await screen.findByTestId('cancel-started')
    expect(screen.queryByTestId('cancel-success')).toBeNull()
  })
})

describe('the access floor', () => {
  it.each(['unauthenticated', 'forbidden'] as const)(
    'renders %s as generic copy, offering a retry',
    async (outcome) => {
      const message = 'Access refused.'
      cancelResourceBooking.mockResolvedValue({ outcome, message })
      renderPanel()
      confirmCancel()

      const error = await screen.findByTestId('cancel-error')
      expect(error.textContent).toBe(message)
      expect(screen.queryByTestId('cancel-success')).toBeNull()
      expect(onCalendarChanged).not.toHaveBeenCalled()
      // Unlike `already_started`, these are not a fact about the booking, so a
      // retry (once whatever access problem is fixed) is still offered.
      expect(screen.getByTestId('cancel-start')).toBeTruthy()
    },
  )
})

describe('invalid_request', () => {
  const detail = 'path.booking_id: Input should be a valid integer'

  it('never renders the diagnostic detail', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    cancelResourceBooking.mockResolvedValue({
      outcome: 'invalid_request',
      detail,
      raw: { detail: [{ msg: 'Input should be a valid integer' }] },
    })
    renderPanel()
    confirmCancel()

    const error = await screen.findByTestId('cancel-error')
    expect(error.textContent).not.toContain(detail)
    expect(document.body.textContent).not.toContain('booking_id')
    // Diagnostics belong in the console, where a developer will find them.
    expect(logged).toHaveBeenCalled()
  })

  it('is not mistaken for a success, and changes nothing', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    cancelResourceBooking.mockResolvedValue({ outcome: 'invalid_request', detail, raw: null })
    renderPanel()
    confirmCancel()

    await screen.findByTestId('cancel-error')
    expect(screen.queryByTestId('cancel-success')).toBeNull()
    // Nothing reached the server, so there is nothing to refetch.
    expect(onCalendarChanged).not.toHaveBeenCalled()
  })
})

describe('failed', () => {
  it('shows generic copy and does not claim the booking is gone', async () => {
    const message = "We couldn't reach the server. Check your connection and try again."
    cancelResourceBooking.mockResolvedValue({ outcome: 'failed', message })
    renderPanel()
    confirmCancel()

    const error = await screen.findByTestId('cancel-error')
    expect(error.textContent).toBe(message)
    expect(screen.queryByTestId('cancel-success')).toBeNull()
    expect(onCalendarChanged).not.toHaveBeenCalled()
  })

  it('leaves the booking selected so the cancel can be retried', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'failed', message: 'Nope.' })
    renderPanel()
    confirmCancel()

    await screen.findByTestId('cancel-error')
    expect(screen.getByTestId('cancel-start')).toBeTruthy()
  })
})

describe('while a request is in flight', () => {
  it('disables the confirm control and cannot be double-submitted', async () => {
    let release: (value: { outcome: 'ok'; data: Booking }) => void = () => {}
    cancelResourceBooking.mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }),
    )

    renderPanel()
    fireEvent.click(screen.getByTestId('cancel-start'))
    const confirm = screen.getByTestId('cancel-confirm') as HTMLButtonElement
    fireEvent.click(confirm)

    await waitFor(() => expect(confirm.disabled).toBe(true))

    // A second DELETE is exactly what produces the `already_cancelled` 409 this
    // panel then has to explain away — better not to send it.
    fireEvent.click(confirm)
    expect(cancelResourceBooking).toHaveBeenCalledTimes(1)

    release({ outcome: 'ok', data: cancelled })
    await screen.findByTestId('cancel-success')
  })

  it('rejects a second click dispatched before React can disable the button', async () => {
    let release: (value: { outcome: 'ok'; data: Booking }) => void = () => {}
    cancelResourceBooking.mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }),
    )

    renderPanel()
    fireEvent.click(screen.getByTestId('cancel-start'))
    const confirm = screen.getByTestId('cancel-confirm') as HTMLButtonElement

    // `fireEvent` wraps each call in its own `act`, so the re-render that sets
    // `disabled` lands between them and the test above never reaches the second
    // handler at all. Dispatching both inside one `act` batches the updates,
    // which is the case `disabled` cannot cover: two handlers running against a
    // DOM that has not been patched yet. Only the in-flight ref stops the
    // second, and a duplicate DELETE is exactly what manufactures the
    // `already_cancelled` 409 this panel then has to explain away.
    await act(async () => {
      confirm.click()
      confirm.click()
    })
    expect(cancelResourceBooking).toHaveBeenCalledTimes(1)

    release({ outcome: 'ok', data: cancelled })
    await screen.findByTestId('cancel-success')
  })
})

describe('when the selection moves', () => {
  it('drops a stale result so it cannot describe a different booking', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'failed', message: 'Nope.' })
    const { rerender } = renderPanel()
    confirmCancel()
    await screen.findByTestId('cancel-error')

    rerender(
      <CancelPanel
        publicId={PUBLIC_ID}
        resourceId={RESOURCE_ID}
        booking={{ ...booking, id: 43 }}
        onCalendarChanged={onCalendarChanged}
      />,
    )

    expect(screen.queryByTestId('cancel-error')).toBeNull()
  })

  it('keeps the confirmation when the booking goes away underneath it', async () => {
    cancelResourceBooking.mockResolvedValue({ outcome: 'ok', data: cancelled })
    const { rerender } = renderPanel()
    confirmCancel()
    await screen.findByTestId('cancel-success')

    // What the real app does the instant the refetch lands: the block is gone,
    // so the selection is null. The receipt must survive it.
    rerender(
      <CancelPanel
        publicId={PUBLIC_ID}
        resourceId={RESOURCE_ID}
        booking={null}
        onCalendarChanged={onCalendarChanged}
      />,
    )

    expect(screen.getByTestId('cancel-success')).toBeTruthy()
    expect(screen.queryByTestId('cancel-start')).toBeNull()
  })
})
