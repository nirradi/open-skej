// @vitest-environment jsdom
/**
 * Tests for the schedule configuration panel: the Space's timezone,
 * operating hours, and slot interval — `DEFERRED.md` item 2.
 *
 * Task 4.13a moved operating hours and slot interval off `resources` and
 * onto `spaces`, so this panel edits one Space's schedule directly. There is
 * nothing left to configure per Resource, so — unlike the 4.12 predecessor
 * this replaces — there is no per-Resource listing here at all.
 *
 * As with every other panel in this dashboard, the hiding in `AdminPage` is a
 * convenience, not the boundary: the write here still has to handle
 * `forbidden` and `conflict` as if it were called directly, because a second
 * admin can act between this component's render and a click.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { updateSpace } from '../api'
import { conflict, forbidden, makeSpace, ok } from './fixtures'
import { SpaceSchedulePanel } from './SpaceSchedulePanel'

vi.mock('../api', () => ({
  updateSpace: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPanel(space = makeSpace(), onSpaceChanged = vi.fn()) {
  render(<SpaceSchedulePanel space={space} onSpaceChanged={onSpaceChanged} />)
  return { onSpaceChanged }
}

describe('SpaceSchedulePanel', () => {
  it('shows the Space timezone, hours, slot interval, and rule parameters', () => {
    renderPanel(
      makeSpace({
        timezone: 'Europe/Berlin',
        opens_at: '07:00:00',
        closes_at: '22:00:00',
        slot_minutes: 30,
        max_duration_minutes: 120,
        booking_horizon_days: 60,
        max_bookings_per_week: 3,
        max_bookings_per_month: 10,
      }),
    )

    expect((screen.getByTestId('timezone-input') as HTMLInputElement).value).toBe(
      'Europe/Berlin',
    )
    expect((screen.getByTestId('opens-input') as HTMLInputElement).value).toBe('07:00')
    expect((screen.getByTestId('closes-input') as HTMLInputElement).value).toBe('22:00')
    expect((screen.getByTestId('slot-input') as HTMLInputElement).value).toBe('30')
    expect((screen.getByTestId('max-duration-input') as HTMLInputElement).value).toBe('120')
    expect((screen.getByTestId('booking-horizon-input') as HTMLInputElement).value).toBe('60')
    expect((screen.getByTestId('max-bookings-per-week-input') as HTMLInputElement).value).toBe(
      '3',
    )
    expect((screen.getByTestId('max-bookings-per-month-input') as HTMLInputElement).value).toBe(
      '10',
    )
  })

  it('saves edited timezone, hours, slot interval, and rule parameters together', async () => {
    const updated = makeSpace({
      timezone: 'Asia/Tokyo',
      opens_at: '08:00:00',
      closes_at: '20:00:00',
      slot_minutes: 45,
      max_duration_minutes: 90,
      booking_horizon_days: 30,
      max_bookings_per_week: 2,
      max_bookings_per_month: 8,
    })
    vi.mocked(updateSpace).mockResolvedValue(ok(updated))
    const { onSpaceChanged } = renderPanel()

    fireEvent.change(screen.getByTestId('timezone-input'), { target: { value: 'Asia/Tokyo' } })
    fireEvent.change(screen.getByTestId('opens-input'), { target: { value: '08:00' } })
    fireEvent.change(screen.getByTestId('closes-input'), { target: { value: '20:00' } })
    fireEvent.change(screen.getByTestId('slot-input'), { target: { value: '45' } })
    fireEvent.change(screen.getByTestId('max-duration-input'), { target: { value: '90' } })
    fireEvent.change(screen.getByTestId('booking-horizon-input'), { target: { value: '30' } })
    fireEvent.change(screen.getByTestId('max-bookings-per-week-input'), {
      target: { value: '2' },
    })
    fireEvent.change(screen.getByTestId('max-bookings-per-month-input'), {
      target: { value: '8' },
    })
    fireEvent.click(screen.getByTestId('schedule-save'))

    expect(vi.mocked(updateSpace)).toHaveBeenCalledWith('sp_7f3a9c', {
      timezone: 'Asia/Tokyo',
      opens_at: '08:00:00',
      closes_at: '20:00:00',
      slot_minutes: 45,
      max_duration_minutes: 90,
      booking_horizon_days: 30,
      max_bookings_per_week: 2,
      max_bookings_per_month: 8,
    })
    await vi.waitFor(() => expect(onSpaceChanged).toHaveBeenCalledWith(updated))
  })

  it('clears hours, slot interval, and rule parameters back to "not enforced" with an explicit null', async () => {
    const updated = makeSpace({
      opens_at: null,
      closes_at: null,
      slot_minutes: null,
      max_duration_minutes: null,
      booking_horizon_days: null,
      max_bookings_per_week: null,
      max_bookings_per_month: null,
    })
    vi.mocked(updateSpace).mockResolvedValue(ok(updated))
    renderPanel(
      makeSpace({
        opens_at: '07:00:00',
        closes_at: '22:00:00',
        slot_minutes: 60,
        max_duration_minutes: 120,
        booking_horizon_days: 60,
        max_bookings_per_week: 3,
        max_bookings_per_month: 10,
      }),
    )

    fireEvent.change(await screen.findByTestId('opens-input'), { target: { value: '' } })
    fireEvent.change(screen.getByTestId('closes-input'), { target: { value: '' } })
    fireEvent.change(screen.getByTestId('slot-input'), { target: { value: '' } })
    fireEvent.change(screen.getByTestId('max-duration-input'), { target: { value: '' } })
    fireEvent.change(screen.getByTestId('booking-horizon-input'), { target: { value: '' } })
    fireEvent.change(screen.getByTestId('max-bookings-per-week-input'), { target: { value: '' } })
    fireEvent.change(screen.getByTestId('max-bookings-per-month-input'), {
      target: { value: '' },
    })
    fireEvent.click(screen.getByTestId('schedule-save'))

    expect(vi.mocked(updateSpace)).toHaveBeenCalledWith('sp_7f3a9c', {
      timezone: 'UTC',
      opens_at: null,
      closes_at: null,
      slot_minutes: null,
      max_duration_minutes: null,
      booking_horizon_days: null,
      max_bookings_per_week: null,
      max_bookings_per_month: null,
    })
  })

  it('shows the server refusal for an unknown zone rather than inventing copy', async () => {
    const detail = "'Not/AZone' is not a known IANA timezone name"
    vi.mocked(updateSpace).mockResolvedValue({ outcome: 'invalid_request', detail, raw: null })
    renderPanel()

    fireEvent.change(screen.getByTestId('timezone-input'), { target: { value: 'Not/AZone' } })
    fireEvent.click(screen.getByTestId('schedule-save'))

    // `invalid_request` is treated as a client-diagnostic bug everywhere else in
    // this dashboard, so it shows the same generic copy here rather than the raw
    // Pydantic detail — consistent with `messageFor`.
    const error = await screen.findByTestId('schedule-error')
    expect(error.textContent).toBeTruthy()
  })

  it('resolves a member acting between render and click to forbidden', async () => {
    vi.mocked(updateSpace).mockResolvedValue(forbidden())
    renderPanel()

    fireEvent.click(screen.getByTestId('schedule-save'))

    const error = await screen.findByTestId('schedule-error')
    expect(error.textContent).toBe("You don't have permission to do that.")
  })

  it('shows the server conflict verbatim for an archived Space', async () => {
    const detail = 'This Space is archived and can no longer be changed.'
    vi.mocked(updateSpace).mockResolvedValue(conflict(detail))
    renderPanel()

    fireEvent.click(screen.getByTestId('schedule-save'))

    const error = await screen.findByTestId('schedule-error')
    expect(error.textContent).toBe(detail)
  })

  it('disables every control on an archived Space', () => {
    renderPanel(makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' }))

    expect(screen.getByTestId('timezone-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('opens-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('closes-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('slot-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('max-duration-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('booking-horizon-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('max-bookings-per-week-input').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('max-bookings-per-month-input').hasAttribute('disabled')).toBe(
      true,
    )
    expect(screen.getByTestId('schedule-save').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('schedule-archived')).toBeTruthy()
  })
})
