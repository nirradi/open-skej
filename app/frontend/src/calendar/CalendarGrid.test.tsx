// @vitest-environment jsdom
/**
 * Component tests for the week grid.
 *
 * Opts into jsdom per-file rather than globally, so the pure-TypeScript suites
 * (the API client, config arithmetic, week and selection maths) keep running in
 * the cheaper `node` environment configured in `vite.config.ts`.
 *
 * The claims under test are the ones a reader cannot verify by inspection:
 * that the grid is genuinely config-driven at two different granularities, that
 * navigation *disables* at the horizon rather than silently no-op-ing, and —
 * most importantly — that a failed fetch does not render as a week of free
 * slots, which is the failure mode that would invite a double booking.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Booking, ListBookingsResult } from '../api'
import type { CalendarConfig } from '../config'
import { CalendarGrid } from './CalendarGrid'
import { slotsPerDayFor } from '../config'
import { slotTestId, startOfWeek, toDateKey } from './week'

const listBookings = vi.hoisted(() => vi.fn())
vi.mock('../api', () => ({ listBookings }))

/** A Wednesday, 14:30 local. Every expectation below is relative to this. */
const NOW = new Date(2026, 6, 22, 14, 30)
/** The Monday of NOW's week. */
const MONDAY = startOfWeek(NOW)

const THIRTY: CalendarConfig = { slotMinutes: 30, openHour: 6, closeHour: 23 }
const TEN: CalendarConfig = { ...THIRTY, slotMinutes: 10 }

/** Resolves `listBookings` with an ok result carrying `bookings`. */
function resolveWith(bookings: Booking[] = []) {
  listBookings.mockResolvedValue({ outcome: 'ok', data: bookings } satisfies ListBookingsResult)
}

/** A confirmed booking over an arbitrary local wall-clock interval. */
function booking(id: number, start: Date, end: Date): Booking {
  return {
    id,
    resource_id: 'default-resource',
    user_id: 'default-user',
    start_at: start.toISOString(),
    end_at: end.toISOString(),
    status: 'confirmed',
    created_at: start.toISOString(),
    cancelled_at: null,
  }
}

/** Renders the grid and waits for the initial load to settle. */
async function renderGrid(props: Partial<React.ComponentProps<typeof CalendarGrid>> = {}) {
  const view = render(<CalendarGrid now={NOW} {...props} />)
  await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
  return view
}

/** The slot button for a day offset from Monday, by index. */
function slot(dayOffset: number, index: number): HTMLButtonElement {
  const day = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + dayOffset)
  return screen.getByTestId(slotTestId(day, index)) as HTMLButtonElement
}

/** Slot indices for the given day that are currently selected. */
function selectedIndices(dayOffset: number, config: CalendarConfig = THIRTY): number[] {
  const indices: number[] = []
  for (let i = 0; i < slotsPerDayFor(config); i += 1) {
    if (slot(dayOffset, i).dataset.selected === 'true') indices.push(i)
  }
  return indices
}

beforeEach(() => {
  listBookings.mockReset()
  resolveWith()
})

afterEach(cleanup)

describe('the grid is driven by config.ts', () => {
  it('renders one row per configured slot at 30 minutes', async () => {
    await renderGrid({ config: THIRTY })
    expect(screen.getByTestId('calendar-grid').dataset.slotsPerDay).toBe('34')
    // The last configured slot exists and the one after it does not — a grid
    // that rendered a fixed count would fail one of these two.
    expect(slot(0, 33)).toBeTruthy()
    expect(screen.queryByTestId(slotTestId(MONDAY, 34))).toBeNull()
  })

  it('renders one row per configured slot at 10 minutes with no other change', async () => {
    await renderGrid({ config: TEN })
    expect(screen.getByTestId('calendar-grid').dataset.slotsPerDay).toBe('102')
    expect(slot(0, 101)).toBeTruthy()
    expect(screen.queryByTestId(slotTestId(MONDAY, 102))).toBeNull()
  })

  it('labels slots from the configured opening hour', async () => {
    await renderGrid({ config: TEN })
    expect(slot(0, 0).getAttribute('aria-label')).toContain('06:00')
    expect(slot(0, 6).getAttribute('aria-label')).toContain('07:00')
  })

  it('renders seven day columns', async () => {
    await renderGrid()
    for (let offset = 0; offset < 7; offset += 1) {
      expect(slot(offset, 0)).toBeTruthy()
    }
  })
})

describe('past slots', () => {
  it('render disabled rather than hidden, so the week does not reflow', async () => {
    await renderGrid()
    // Monday 06:00 is three days behind NOW but still present in the grid.
    const monday = slot(0, 0)
    expect(monday).toBeTruthy()
    expect(monday.disabled).toBe(true)
    expect(monday.dataset.blocked).toBe('past')
  })

  it('disables earlier slots on today but not later ones', async () => {
    await renderGrid()
    // NOW is 14:30 on Wednesday (day offset 2). Index 16 is 14:00, index 18 is
    // 15:00. Asserting both directions makes this non-vacuous: a component that
    // disabled everything, or nothing, fails one half.
    expect(slot(2, 16).disabled).toBe(true)
    expect(slot(2, 16).dataset.blocked).toBe('past')
    expect(slot(2, 18).disabled).toBe(false)
    expect(slot(2, 18).dataset.blocked).toBeUndefined()
  })

  it('leaves a future day entirely enabled', async () => {
    await renderGrid()
    expect(slot(4, 0).disabled).toBe(false)
  })
})

describe('navigation bounds', () => {
  it('disables previous on the current week', async () => {
    await renderGrid()
    expect((screen.getByTestId('calendar-prev-week') as HTMLButtonElement).disabled).toBe(true)
  })

  it('enables previous once the user has paged forward', async () => {
    await renderGrid()
    fireEvent.click(screen.getByTestId('calendar-next-week'))
    await waitFor(() =>
      expect((screen.getByTestId('calendar-prev-week') as HTMLButtonElement).disabled).toBe(false),
    )
  })

  it('disables next at the horizon, and the last reachable week is inside it', async () => {
    await renderGrid()
    const next = () => screen.getByTestId('calendar-next-week') as HTMLButtonElement

    // Page forward until the control disables. The bound must be reached by
    // *disabling*, not by clicks that quietly do nothing, so the loop is capped
    // well above the ~9 weeks 60 days spans and the cap is asserted separately.
    let clicks = 0
    while (!next().disabled && clicks < 30) {
      fireEvent.click(next())
      clicks += 1
      await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
    }

    expect(next().disabled).toBe(true)
    // 60 days is between 8 and 10 week-pages from a mid-week start; a control
    // that never disabled would have hit the cap instead.
    expect(clicks).toBeGreaterThan(5)
    expect(clicks).toBeLessThan(12)
  })

  it('disables the slots past the horizon on the final reachable week', async () => {
    await renderGrid()
    const next = () => screen.getByTestId('calendar-next-week') as HTMLButtonElement
    while (!next().disabled) {
      fireEvent.click(next())
      await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
    }

    // NOW is Wednesday 2026-07-22 14:30, so the horizon is 2026-09-20 14:30 —
    // the Sunday of the last reachable week (which begins Monday 2026-09-14).
    // Everything up to that instant is bookable; everything after it is not.
    const horizonDay = new Date(2026, 8, 20)
    const at = (day: Date, index: number) =>
      (screen.getByTestId(slotTestId(day, index)) as HTMLButtonElement).dataset.blocked

    // 14:00 on the horizon day is inside the horizon, 15:00 is past it.
    expect(at(horizonDay, 16)).toBeUndefined()
    expect(at(horizonDay, 18)).toBe('beyond-horizon')
    // The day before is bookable right up to closing — the negative control
    // proving the assertion above is not just "everything late is blocked".
    expect(at(new Date(2026, 8, 19), 33)).toBeUndefined()
  })
})

describe('existing bookings', () => {
  it('renders a block per booking and disables the slots it covers', async () => {
    const start = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4, 9, 0)
    const end = new Date(start.getTime() + 90 * 60_000)
    resolveWith([booking(7, start, end)])
    await renderGrid()

    expect(screen.getByTestId('booking-7')).toBeTruthy()
    // 09:00–10:30 at 30-minute slots is indices 6, 7, 8 from a 06:00 open.
    expect(slot(4, 6).dataset.blocked).toBe('booked')
    expect(slot(4, 7).dataset.blocked).toBe('booked')
    expect(slot(4, 8).dataset.blocked).toBe('booked')
    // Half-open: the slot starting exactly at the booking's end is free.
    expect(slot(4, 9).dataset.blocked).toBeUndefined()
    expect(slot(4, 5).dataset.blocked).toBeUndefined()
  })

  it('requests exactly the displayed week', async () => {
    await renderGrid()
    const [from, to] = listBookings.mock.calls[0]
    expect(toDateKey(from as Date)).toBe(toDateKey(MONDAY))
    expect((to as Date).getTime() - (from as Date).getTime()).toBe(7 * 86400_000)
  })
})

describe('a failed load', () => {
  it('surfaces an error instead of an empty, apparently-free calendar', async () => {
    listBookings.mockResolvedValue({
      outcome: 'failed',
      message: "We couldn't reach the server.",
    } satisfies ListBookingsResult)
    await renderGrid()

    expect(screen.getByTestId('calendar-error')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain("We couldn't reach the server.")
    // The point of the test: the grid is still there, but nothing in it is
    // bookable. An empty grid of *enabled* slots is the double-booking trap.
    expect(screen.getByTestId('calendar-grid')).toBeTruthy()
    expect(slot(4, 0).disabled).toBe(true)
    expect(slot(4, 0).dataset.blocked).toBe('unavailable')
  })

  it('treats an invalid_request as an error too, without showing the raw detail', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    listBookings.mockResolvedValue({
      outcome: 'invalid_request',
      detail: 'query.from: input should be a valid datetime',
      raw: null,
    } satisfies ListBookingsResult)
    await renderGrid()

    const alert = screen.getByRole('alert')
    expect(alert).toBeTruthy()
    // Diagnostic text is for the console, not for the user.
    expect(alert.textContent).not.toContain('input should be a valid datetime')
    expect(slot(4, 0).disabled).toBe(true)
  })

  it('retries on demand and recovers', async () => {
    listBookings.mockResolvedValueOnce({ outcome: 'failed', message: 'Nope.' })
    await renderGrid()
    expect(screen.getByTestId('calendar-error')).toBeTruthy()

    resolveWith()
    fireEvent.click(screen.getByTestId('calendar-retry'))
    await waitFor(() => expect(screen.queryByTestId('calendar-error')).toBeNull())
    expect(slot(4, 0).disabled).toBe(false)
  })

  it('does not select when a slot is clicked while the load has failed', async () => {
    listBookings.mockResolvedValue({ outcome: 'failed', message: 'Nope.' })
    await renderGrid()
    fireEvent.pointerDown(slot(4, 4))
    expect(screen.queryByTestId('calendar-selection')).toBeNull()
  })
})

describe('selection', () => {
  it('selects a single slot on click', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerUp(window)
    expect(selectedIndices(4)).toEqual([4])
  })

  it('selects a contiguous range when dragged downward', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerOver(slot(4, 5))
    fireEvent.pointerOver(slot(4, 7))
    fireEvent.pointerUp(window)
    expect(selectedIndices(4)).toEqual([4, 5, 6, 7])
  })

  it('selects a contiguous range when dragged upward', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 8))
    fireEvent.pointerOver(slot(4, 6))
    fireEvent.pointerUp(window)
    expect(selectedIndices(4)).toEqual([6, 7, 8])
  })

  it('stops at a booked slot instead of selecting through it', async () => {
    const day = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4)
    const start = new Date(day.getFullYear(), day.getMonth(), day.getDate(), 10, 0)
    resolveWith([booking(3, start, new Date(start.getTime() + 30 * 60_000))])
    await renderGrid()

    // Index 8 is 10:00, the booked one. Dragging 5 → 12 must stop at 7.
    fireEvent.pointerDown(slot(4, 5))
    fireEvent.pointerOver(slot(4, 12))
    fireEvent.pointerUp(window)
    expect(selectedIndices(4)).toEqual([5, 6, 7])
  })

  it('ignores a drag onto another day column', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerOver(slot(5, 9))
    fireEvent.pointerUp(window)
    expect(selectedIndices(4)).toEqual([4])
    expect(selectedIndices(5)).toEqual([])
  })

  it('does not extend after the pointer is released', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerUp(window)
    fireEvent.pointerOver(slot(4, 9))
    expect(selectedIndices(4)).toEqual([4])
  })

  it('reports the selected interval as wall-clock times', async () => {
    const onSelectionChange = vi.fn()
    await renderGrid({ onSelectionChange })
    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerOver(slot(4, 5))
    fireEvent.pointerUp(window)

    const interval = onSelectionChange.mock.calls.at(-1)?.[0] as { start: Date; end: Date }
    expect(interval.start.getHours()).toBe(8)
    expect(interval.start.getMinutes()).toBe(0)
    // Two 30-minute slots, so the range ends at the *end* of the second.
    expect(interval.end.getHours()).toBe(9)
    expect(interval.end.getMinutes()).toBe(0)
  })

  it('clears the selection when the week changes', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerUp(window)
    expect(screen.getByTestId('calendar-selection')).toBeTruthy()

    await act(async () => {
      fireEvent.click(screen.getByTestId('calendar-next-week'))
    })
    await waitFor(() => expect(screen.queryByTestId('calendar-selection')).toBeNull())
  })
})
