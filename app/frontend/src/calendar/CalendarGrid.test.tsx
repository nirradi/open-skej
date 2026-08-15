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

import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Booking, ListResourceBookingsResult } from '../api'
import type { CalendarConfig } from '../config'
import { CalendarGrid } from './CalendarGrid'
import { buildWeekSchedule, slotsPerDayFor, uniformWeekSchedule } from '../config'
import { addDays, DAYS_PER_WEEK, slotTestId, startOfWeek, toDateKey } from './week'
import { SYSTEM_TIME_ZONE } from '../timezone'

const listResourceBookings = vi.hoisted(() => vi.fn())
vi.mock('../api', () => ({ listResourceBookings }))

/** A Wednesday, 14:30 local. Every expectation below is relative to this. */
const NOW = new Date(2026, 6, 22, 14, 30)
/** The Monday of NOW's week. */
const MONDAY = startOfWeek(NOW)

// Every config below except the dedicated cross-zone `describe` further down
// pins `timeZone` to the environment's own zone, so "the Space's clock" and
// "the environment's clock" are the same thing here — exactly the assumption
// this suite always ran under, preserved on purpose so the existing
// assertions (`.disabled`, slot indices, aria-labels built from local wall
// clock) still mean what they say.
const SYSTEM_TZ = SYSTEM_TIME_ZONE

const THIRTY: CalendarConfig = {
  sessionMinutes: 30,
  openMinutes: null,
  closeMinutes: null,
  timeZone: SYSTEM_TZ,
  anchorMinutes: 0,
}
const TEN: CalendarConfig = { ...THIRTY, sessionMinutes: 10 }
/** 09:00-17:00, 30-minute slots — for the tests that need a real hours window. */
const NINE_TO_FIVE: CalendarConfig = {
  sessionMinutes: 30,
  openMinutes: 9 * 60,
  closeMinutes: 17 * 60,
  timeZone: SYSTEM_TZ,
  anchorMinutes: 0,
}

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'
const RESOURCE_ID = 1

/** Resolves `listResourceBookings` with an ok result carrying `bookings`. */
function resolveWith(bookings: Booking[] = []) {
  listResourceBookings.mockResolvedValue({
    outcome: 'ok',
    data: bookings,
  } satisfies ListResourceBookingsResult)
}

/** A confirmed booking over an arbitrary local wall-clock interval. */
function booking(id: number, start: Date, end: Date, mine = true): Booking {
  return {
    id,
    resource_id: 1,
    user_id: mine ? 1 : 2,
    mine,
    start_at: start.toISOString(),
    end_at: end.toISOString(),
    status: 'confirmed',
    created_at: start.toISOString(),
    cancelled_at: null,
  }
}

type GridHarnessProps = Omit<Partial<React.ComponentProps<typeof CalendarGrid>>, 'weekStart'> & {
  initialWeekStart?: Date
}

/**
 * Wraps `CalendarGrid` with the sliver of state a real caller owns since task
 * 5.8: `weekStart` is a prop the grid reports navigation upward from, never
 * state it holds itself, so exercising Previous / Next / "This week" through
 * this suite needs something to feed the reported value back in — the
 * minimal version of what `ResourceCalendarPage` does with `?week=`.
 */
function GridHarness({ initialWeekStart = MONDAY, onWeekChange, ...rest }: GridHarnessProps) {
  const [weekStart, setWeekStart] = useState(initialWeekStart)
  return (
    <CalendarGrid
      publicId={PUBLIC_ID}
      resourceId={RESOURCE_ID}
      now={NOW}
      {...rest}
      weekStart={weekStart}
      onWeekChange={(next) => {
        setWeekStart(next)
        onWeekChange?.(next)
      }}
    />
  )
}

/** Renders the grid (behind `GridHarness`) and waits for the initial load to settle. */
async function renderGrid(props: GridHarnessProps = {}) {
  const view = render(<GridHarness {...props} />)
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
  listResourceBookings.mockReset()
  resolveWith()
})

afterEach(cleanup)

describe('the grid is driven by config.ts', () => {
  it('renders one row per day for every 30-minute slot', async () => {
    await renderGrid({ schedule: uniformWeekSchedule(THIRTY) })
    // The grid always spans the full day (task 5.9) — 24 hours at 30 minutes
    // is 48 rows, whatever a Space's own hours say.
    expect(screen.getByTestId('calendar-grid').dataset.slotsPerDay).toBe('48')
    // The last configured slot exists and the one after it does not — a grid
    // that rendered a fixed count would fail one of these two.
    expect(slot(0, 47)).toBeTruthy()
    expect(screen.queryByTestId(slotTestId(MONDAY, 48))).toBeNull()
  })

  it('renders one row per configured slot at 10 minutes with no other change', async () => {
    await renderGrid({ schedule: uniformWeekSchedule(TEN) })
    expect(screen.getByTestId('calendar-grid').dataset.slotsPerDay).toBe('144')
    expect(slot(0, 143)).toBeTruthy()
    expect(screen.queryByTestId(slotTestId(MONDAY, 144))).toBeNull()
  })

  it('labels slots starting at midnight, whatever the configured hours', async () => {
    await renderGrid({ schedule: uniformWeekSchedule(TEN) })
    expect(slot(0, 0).getAttribute('aria-label')).toContain('00:00')
    expect(slot(0, 6).getAttribute('aria-label')).toContain('01:00')
  })

  it('renders seven day columns', async () => {
    await renderGrid()
    for (let offset = 0; offset < 7; offset += 1) {
      expect(slot(offset, 0)).toBeTruthy()
    }
  })
})

describe('a heterogeneous week (task 6.9)', () => {
  // Monday resolves to 15-minute slots; every other day resolves to the
  // ordinary 30-minute default — genuinely different slot sizes inside the
  // same visible week, the shape `applies_to` makes possible once a rule can
  // be scoped to particular weekdays or dates rather than the whole Space.
  function heterogeneousEntries(
    coherenceIssues: Partial<Record<number, string>> = {},
  ): Parameters<typeof buildWeekSchedule>[0] {
    return Array.from({ length: DAYS_PER_WEEK }, (_, i) => ({
      date: toDateKey(addDays(MONDAY, i)),
      session_minutes: i === 0 ? 15 : 30,
      opens_at: null,
      closes_at: null,
      coherence_issue: coherenceIssues[i] ?? null,
      anchor_minutes: null,
    }))
  }

  it('renders the shared axis at the finest slot size configured anywhere in the week', async () => {
    const schedule = buildWeekSchedule(heterogeneousEntries(), SYSTEM_TZ)
    await renderGrid({ schedule })

    // 1440 / 15 = 96 — the axis is Monday's 15-minute grid, the finest of the
    // two configured this week, not the 30-minute default every other day uses.
    expect(screen.getByTestId('calendar-grid').dataset.slotsPerDay).toBe('96')
  })

  it("lays out each day's own buttons at that day's own slot size, not the shared axis's", async () => {
    const schedule = buildWeekSchedule(heterogeneousEntries(), SYSTEM_TZ)
    await renderGrid({ schedule })

    // Monday (15-minute) renders 96 of its own buttons — flush with the axis.
    expect(slot(0, 95)).toBeTruthy()
    expect(screen.queryByTestId(slotTestId(MONDAY, 96))).toBeNull()

    // Tuesday (the ordinary 30-minute default) renders only 48 — half the
    // axis row count. It shares the same total `dayHeight` as Monday (both
    // fill the identical pixel height in normal document flow, per
    // `config.ts`'s `finestSessionMinutes` docblock) but not its row lines. This
    // is the readability limit the PR description records as a finding: a
    // 30-minute day beside a 15-minute one lines up on overall height, not on
    // where each row falls.
    const tuesday = addDays(MONDAY, 1)
    expect(slot(1, 47)).toBeTruthy()
    expect(screen.queryByTestId(slotTestId(tuesday, 48))).toBeNull()
  })

  it("shows a day's own coherence issue as an advisory note without blocking that day's grid", async () => {
    const message = 'Opening time must land on a 15-minute slot boundary.'
    const schedule = buildWeekSchedule(heterogeneousEntries({ 4: message }), SYSTEM_TZ)
    await renderGrid({ schedule })

    const friday = addDays(MONDAY, 4)
    expect(screen.getByTestId(`calendar-notice-${toDateKey(friday)}`).textContent).toBe(message)

    // Advisory only: Friday's own slots are still rendered and selectable —
    // day offset 4 is entirely in the future relative to `NOW` (Wednesday),
    // so nothing else would block index 0.
    expect(slot(4, 0)).toBeTruthy()
    expect(slot(4, 0).disabled).toBe(false)

    // A day with no issue renders the identical testid, empty — never
    // absent, so "no issue" is distinguishable from "nothing rendered yet".
    expect(screen.getByTestId(`calendar-notice-${toDateKey(MONDAY)}`).textContent).toBe('')
  })
})

describe('the DEFERRED.md item 19 repro: a Space in one zone, a viewer in another', () => {
  // Exactly the repro recorded in the deferred entry: a Space at
  // `Europe/Berlin`, open 13:00-22:00. August 3 2026 is a Monday, and Berlin
  // is on CEST (UTC+2) that week, so the Space's 13:00 opening is 11:00Z —
  // never 10:00Z, which is what sending the *viewer's* own zone (the item 19
  // repro used Asia/Jerusalem, UTC+3) would have produced.
  const BERLIN: CalendarConfig = {
    sessionMinutes: 30,
    openMinutes: 13 * 60,
    closeMinutes: 22 * 60,
    timeZone: 'Europe/Berlin',
    anchorMinutes: 0,
  }
  const AUGUST_MONDAY = new Date(2026, 7, 3)

  it('renders the 13:00 row as bookable, not greyed', async () => {
    await renderGrid({ schedule: uniformWeekSchedule(BERLIN), initialWeekStart: AUGUST_MONDAY })
    // 13:00 at 30-minute slots, from midnight, is index 26.
    const row = screen.getByTestId(slotTestId(AUGUST_MONDAY, 26))
    expect(row.dataset.blocked).toBeUndefined()
    expect((row as HTMLButtonElement).disabled).toBe(false)
    // The slot just before opening is still greyed — the grid draws a real
    // boundary at 13:00, not an accidentally-permissive one.
    expect(screen.getByTestId(slotTestId(AUGUST_MONDAY, 25)).dataset.blocked).toBe('out-of-hours')
  })

  it('selects the exact instant the backend accepts — 11:00Z, not 10:00Z', async () => {
    const onSelectionChange = vi.fn()
    await renderGrid({ schedule: uniformWeekSchedule(BERLIN), initialWeekStart: AUGUST_MONDAY, onSelectionChange })

    const row = screen.getByTestId(slotTestId(AUGUST_MONDAY, 26))
    fireEvent.pointerDown(row)
    fireEvent.pointerUp(window)

    const interval = onSelectionChange.mock.calls.at(-1)?.[0] as { start: Date; end: Date }
    expect(interval.start.toISOString()).toBe('2026-08-03T11:00:00.000Z')
  })
})

describe('out-of-hours slots', () => {
  it('greys slots before opening and from closing onward, without disappearing', async () => {
    await renderGrid({ schedule: uniformWeekSchedule(NINE_TO_FIVE) })
    // 08:30 is one slot before opening; 09:00 is the first open slot.
    expect(slot(4, 17).dataset.blocked).toBe('out-of-hours')
    expect(slot(4, 17).disabled).toBe(true)
    expect(slot(4, 18).dataset.blocked).toBeUndefined()
    // 17:00 is closing — the slot starting there is already closed, matching
    // "a booking may end exactly at closing", never start there.
    expect(slot(4, 34).dataset.blocked).toBe('out-of-hours')
  })

  it('still renders the full day rather than clipping it to the open window', async () => {
    await renderGrid({ schedule: uniformWeekSchedule(NINE_TO_FIVE) })
    expect(screen.getByTestId('calendar-grid').dataset.slotsPerDay).toBe('48')
    expect(slot(4, 0)).toBeTruthy()
    expect(slot(4, 47)).toBeTruthy()
  })

  it('keeps a booking on screen even though later-narrowed hours now grey its row', async () => {
    // A booking made at 07:00, before an admin narrowed the Space to 09:00-17:00.
    // The row it sits on is greyed, not gone — it must still show the booking
    // that is genuinely still on the calendar.
    const day = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4)
    const start = new Date(day.getFullYear(), day.getMonth(), day.getDate(), 7, 0)
    resolveWith([booking(21, start, new Date(start.getTime() + 30 * 60_000))])
    await renderGrid({ schedule: uniformWeekSchedule(NINE_TO_FIVE) })

    expect(screen.getByTestId('booking-21')).toBeTruthy()
    expect(slot(4, 14).dataset.blocked).toBe('out-of-hours')
  })
})

describe('past slots', () => {
  it('render disabled rather than hidden, so the week does not reflow', async () => {
    await renderGrid()
    // Monday 00:00 is three days behind NOW but still present in the grid.
    const monday = slot(0, 0)
    expect(monday).toBeTruthy()
    expect(monday.disabled).toBe(true)
    expect(monday.dataset.blocked).toBe('past')
  })

  it('disables earlier slots on today but not later ones', async () => {
    await renderGrid()
    // NOW is 14:30 on Wednesday (day offset 2). Index 28 is 14:00, index 30 is
    // 15:00. Asserting both directions makes this non-vacuous: a component that
    // disabled everything, or nothing, fails one half.
    expect(slot(2, 28).disabled).toBe(true)
    expect(slot(2, 28).dataset.blocked).toBe('past')
    expect(slot(2, 30).disabled).toBe(false)
    expect(slot(2, 30).dataset.blocked).toBeUndefined()
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

  it('reports navigation upward rather than paging itself', async () => {
    // The grid takes `weekStart` as a prop and must not own it — proved here
    // with a bare render (no `GridHarness`, so nothing feeds the reported
    // value back in): if the click still changed what's on screen, the grid
    // would be pacing an internal copy of the week regardless of the prop.
    const onWeekChange = vi.fn()
    render(<CalendarGrid publicId={PUBLIC_ID} resourceId={RESOURCE_ID} now={NOW} weekStart={MONDAY} onWeekChange={onWeekChange} />)
    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())

    fireEvent.click(screen.getByTestId('calendar-next-week'))
    expect(onWeekChange).toHaveBeenCalledWith(addDays(MONDAY, DAYS_PER_WEEK))
    // No harness is feeding the reported value back in, so the label must
    // still read the original week.
    expect(screen.getByTestId('calendar-week-label').textContent).toContain('Jul 20')
  })

  it('enables previous once the user has paged forward', async () => {
    await renderGrid()
    fireEvent.click(screen.getByTestId('calendar-next-week'))
    await waitFor(() =>
      expect((screen.getByTestId('calendar-prev-week') as HTMLButtonElement).disabled).toBe(false),
    )
  })

  describe('"This week"', () => {
    it('is disabled on the current week', async () => {
      await renderGrid()
      expect((screen.getByTestId('calendar-this-week') as HTMLButtonElement).disabled).toBe(true)
    })

    it('returns to the current week in one click from four weeks out', async () => {
      await renderGrid()
      const next = screen.getByTestId('calendar-next-week')
      for (let i = 0; i < 4; i += 1) {
        fireEvent.click(next)
        await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
      }
      expect(screen.getByTestId('calendar-week-label').textContent).not.toContain('Jul 20')

      const thisWeek = screen.getByTestId('calendar-this-week') as HTMLButtonElement
      expect(thisWeek.disabled).toBe(false)
      fireEvent.click(thisWeek)

      await waitFor(() =>
        expect(screen.getByTestId('calendar-week-label').textContent).toContain('Jul 20'),
      )
      expect((screen.getByTestId('calendar-this-week') as HTMLButtonElement).disabled).toBe(true)
    })

    it('does nothing while already on the current week', async () => {
      const onWeekChange = vi.fn()
      await renderGrid({ onWeekChange })
      fireEvent.click(screen.getByTestId('calendar-this-week'))
      expect(onWeekChange).not.toHaveBeenCalled()
    })
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
    expect(at(horizonDay, 28)).toBeUndefined()
    expect(at(horizonDay, 30)).toBe('beyond-horizon')
    // The day before is bookable right up to the end of the day — the
    // negative control proving the assertion above is not just "everything
    // late is blocked".
    expect(at(new Date(2026, 8, 19), 47)).toBeUndefined()
  })
})

describe('existing bookings', () => {
  it('renders a block per booking and disables the slots it covers', async () => {
    const start = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4, 9, 0)
    const end = new Date(start.getTime() + 90 * 60_000)
    resolveWith([booking(7, start, end)])
    await renderGrid()

    expect(screen.getByTestId('booking-7')).toBeTruthy()
    // 09:00–10:30 at 30-minute slots, from midnight, is indices 18, 19, 20.
    expect(slot(4, 18).dataset.blocked).toBe('booked')
    expect(slot(4, 19).dataset.blocked).toBe('booked')
    expect(slot(4, 20).dataset.blocked).toBe('booked')
    // Half-open: the slot starting exactly at the booking's end is free.
    expect(slot(4, 21).dataset.blocked).toBeUndefined()
    expect(slot(4, 17).dataset.blocked).toBeUndefined()
  })

  it('renders a not-mine booking visually distinct, with an aria-label that says so', async () => {
    const start = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4, 9, 0)
    const end = new Date(start.getTime() + 60 * 60_000)
    resolveWith([booking(8, start, end, false)])
    await renderGrid()

    const block = screen.getByTestId('booking-8')
    expect(block.getAttribute('aria-label')).toContain('someone else')
    // Not the caller's own indigo — a distinct colour is the whole assertion.
    expect(block.className).not.toContain('indigo')
  })

  it('renders the caller\'s own booking as before: indigo, with no "someone else" copy', async () => {
    const start = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4, 9, 0)
    const end = new Date(start.getTime() + 60 * 60_000)
    resolveWith([booking(9, start, end, true)])
    await renderGrid()

    const block = screen.getByTestId('booking-9')
    expect(block.getAttribute('aria-label')).not.toContain('someone else')
    expect(block.className).toContain('indigo')
  })

  it('requests exactly the displayed week, scoped to the Space and Resource', async () => {
    await renderGrid()
    const [publicId, resourceId, from, to] = listResourceBookings.mock.calls[0]
    expect(publicId).toBe(PUBLIC_ID)
    expect(resourceId).toBe(RESOURCE_ID)
    expect(toDateKey(from as Date)).toBe(toDateKey(MONDAY))
    expect((to as Date).getTime() - (from as Date).getTime()).toBe(7 * 86400_000)
  })
})

describe('selecting a booking to cancel it', () => {
  /** A one-hour booking on Friday at 14:00 — indices 28 and 29 at 30 minutes. */
  const FRIDAY = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4)
  const START = new Date(FRIDAY.getFullYear(), FRIDAY.getMonth(), FRIDAY.getDate(), 14, 0)

  function withBooking(id = 11) {
    resolveWith([booking(id, START, new Date(START.getTime() + 60 * 60_000))])
  }

  it('reports the booking when its block is clicked', async () => {
    const onBookingSelect = vi.fn()
    withBooking()
    await renderGrid({ onBookingSelect })

    fireEvent.click(screen.getByTestId('booking-11'))
    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toMatchObject({ id: 11 })
  })

  it('reports null when the same block is clicked again', async () => {
    const onBookingSelect = vi.fn()
    withBooking()
    await renderGrid({ onBookingSelect })

    fireEvent.click(screen.getByTestId('booking-11'))
    fireEvent.click(screen.getByTestId('booking-11'))
    // Clicking through is how the panel is put away without a dismiss control
    // that would have to reach back into the grid to clear its state.
    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toBeNull()
  })

  it('retracts a range selection, because the two are different questions', async () => {
    withBooking()
    await renderGrid()

    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerUp(window)
    expect(selectedIndices(4)).toEqual([4])

    fireEvent.click(screen.getByTestId('booking-11'))
    expect(selectedIndices(4)).toEqual([])
    expect(screen.getByTestId('booking-11').dataset.selected).toBe('true')
  })

  it('is retracted in turn when a drag starts on a free slot', async () => {
    const onBookingSelect = vi.fn()
    withBooking()
    await renderGrid({ onBookingSelect })

    fireEvent.click(screen.getByTestId('booking-11'))
    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerUp(window)

    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toBeNull()
    expect(screen.getByTestId('booking-11').dataset.selected).toBeUndefined()
  })

  it('reports null once a refresh removes the booking it named', async () => {
    const onBookingSelect = vi.fn()
    withBooking()
    const { rerender } = await renderGrid({ onBookingSelect, refreshToken: 0 })
    fireEvent.click(screen.getByTestId('booking-11'))
    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toMatchObject({ id: 11 })

    // What a successful cancel does: the week is refetched and comes back
    // without it. Nothing may still be offering to cancel a booking that is
    // gone, and the freed slots must be selectable again.
    resolveWith([])
    await act(async () => {
      rerender(
        <CalendarGrid
          publicId={PUBLIC_ID}
          resourceId={RESOURCE_ID}
          now={NOW}
          weekStart={MONDAY}
          onBookingSelect={onBookingSelect}
          refreshToken={1}
        />,
      )
    })
    await waitFor(() => expect(screen.queryByTestId('booking-11')).toBeNull())

    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toBeNull()
    expect(slot(4, 28).disabled).toBe(false)
  })

  it('drops the selected booking on a refresh even if it is still there', async () => {
    // A refresh means the week on screen is authoritative again, so both
    // selections reset — the same unconditional treatment the range selection
    // gets. Distinct from the test above: there the booking vanishes, so
    // deriving it from the loaded list would have been enough on its own. Here
    // it survives the refetch, and only the explicit reset drops it.
    const onBookingSelect = vi.fn()
    withBooking()
    const { rerender } = await renderGrid({ onBookingSelect, refreshToken: 0 })
    fireEvent.click(screen.getByTestId('booking-11'))
    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toMatchObject({ id: 11 })

    await act(async () => {
      rerender(
        <CalendarGrid
          publicId={PUBLIC_ID}
          resourceId={RESOURCE_ID}
          now={NOW}
          weekStart={MONDAY}
          onBookingSelect={onBookingSelect}
          refreshToken={1}
        />,
      )
    })
    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())

    expect(screen.getByTestId('booking-11')).toBeTruthy()
    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toBeNull()
  })

  it('does not shadow the grid with a pointer-events-none block', async () => {
    // Deliberately a class-name assertion, and the only test here that is.
    // jsdom performs no hit-testing, so the *effect* of `pointer-events` is
    // unobservable in this suite: restoring `pointer-events-none` would make
    // cancelling impossible in a browser while every behavioural test above
    // still passed, because `fireEvent` dispatches straight at its target.
    // Pinning the mechanism is the only guard available; task 1.9's Playwright
    // suite is what will exercise the real thing.
    withBooking()
    await renderGrid()

    const block = screen.getByTestId('booking-11')
    expect(block.tagName).toBe('BUTTON')
    expect(block.className).not.toContain('pointer-events-none')
  })

  it('drops the selected booking when the week changes', async () => {
    const onBookingSelect = vi.fn()
    withBooking()
    await renderGrid({ onBookingSelect })
    fireEvent.click(screen.getByTestId('booking-11'))

    await act(async () => {
      fireEvent.click(screen.getByTestId('calendar-next-week'))
    })
    await waitFor(() => expect(onBookingSelect.mock.calls.at(-1)?.[0]).toBeNull())
  })

  it('drops the selected booking when `weekStart` changes with no click at all', async () => {
    // A bare render, no `GridHarness`: the prop itself is what moves — the
    // shape Back, a pasted link, or any other caller-driven `?week=` change
    // takes, none of which fire a click this component ever sees.
    const onBookingSelect = vi.fn()
    withBooking()
    const { rerender } = render(
      <CalendarGrid
        publicId={PUBLIC_ID}
        resourceId={RESOURCE_ID}
        now={NOW}
        weekStart={MONDAY}
        onBookingSelect={onBookingSelect}
      />,
    )
    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
    fireEvent.click(screen.getByTestId('booking-11'))
    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toMatchObject({ id: 11 })

    rerender(
      <CalendarGrid
        publicId={PUBLIC_ID}
        resourceId={RESOURCE_ID}
        now={NOW}
        weekStart={addDays(MONDAY, DAYS_PER_WEEK)}
        onBookingSelect={onBookingSelect}
      />,
    )
    // Reset in the same render the prop changed, before the new week's fetch
    // even resolves.
    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toBeNull()

    // And it stays null once the new week's bookings load — the mock hands
    // back a booking with the same id regardless of which week was asked for,
    // so this is what proves the reset is explicit rather than merely "the
    // booking happened to vanish", the same distinction the refresh-token
    // tests above draw.
    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toBeNull()
  })

  it('drops a range selection when `weekStart` changes with no click at all', async () => {
    const { rerender } = render(
      <CalendarGrid publicId={PUBLIC_ID} resourceId={RESOURCE_ID} now={NOW} weekStart={MONDAY} />,
    )
    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerUp(window)
    expect(screen.getByTestId('calendar-selection')).toBeTruthy()

    rerender(
      <CalendarGrid
        publicId={PUBLIC_ID}
        resourceId={RESOURCE_ID}
        now={NOW}
        weekStart={addDays(MONDAY, DAYS_PER_WEEK)}
      />,
    )
    expect(screen.queryByTestId('calendar-selection')).toBeNull()
  })

  it('never covers a slot that was selectable', async () => {
    // The invariant that makes clickable blocks safe. A block sits on top of
    // the slot buttons and now intercepts their pointer events, which would be
    // a real hazard if it could shadow a slot the user was allowed to drag
    // through. It cannot: the block is laid out from the same interval that
    // makes every slot it touches `blocked === 'booked'`.
    //
    // jsdom has no layout and `fireEvent` dispatches straight at its target, so
    // hit-testing itself is unobservable here. Asserting the geometric
    // invariant is what actually holds the guarantee up.
    withBooking()
    await renderGrid({ schedule: uniformWeekSchedule(THIRTY) })

    const slotHeight = parseFloat(slot(4, 0).style.height)
    const block = screen.getByTestId('booking-11')
    const top = parseFloat(block.style.top)
    const bottom = top + parseFloat(block.style.height)

    let covered = 0
    for (let index = 0; index < slotsPerDayFor(THIRTY); index += 1) {
      const slotTop = index * slotHeight
      if (slotTop < bottom && top < slotTop + slotHeight) {
        covered += 1
        expect(slot(4, index).disabled).toBe(true)
      }
    }
    // Non-vacuous: a block of zero height would satisfy the loop trivially.
    expect(covered).toBe(2)
  })
})

describe('a failed load', () => {
  it('surfaces an error instead of an empty, apparently-free calendar', async () => {
    listResourceBookings.mockResolvedValue({
      outcome: 'failed',
      message: "We couldn't reach the server.",
    } satisfies ListResourceBookingsResult)
    await renderGrid()

    expect(screen.getByTestId('calendar-error')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain("We couldn't reach the server.")
    // The point of the test: the grid is still there, but nothing in it is
    // bookable. An empty grid of *enabled* slots is the double-booking trap.
    expect(screen.getByTestId('calendar-grid')).toBeTruthy()
    expect(slot(4, 0).disabled).toBe(true)
    expect(slot(4, 0).dataset.blocked).toBe('unavailable')
  })

  it.each(['unauthenticated', 'forbidden', 'not_found'] as const)(
    'treats %s as a load error too, fail-closed like a network failure',
    async (outcome) => {
      listResourceBookings.mockResolvedValue({
        outcome,
        message: 'Access refused.',
      } satisfies ListResourceBookingsResult)
      await renderGrid()

      expect(screen.getByTestId('calendar-error')).toBeTruthy()
      // Every slot is unavailable, exactly as an ordinary network failure would
      // leave it — the grid has no trustworthy answer about what is booked.
      expect(slot(4, 0).disabled).toBe(true)
      expect(slot(4, 0).dataset.blocked).toBe('unavailable')
    },
  )

  it('treats an invalid_request as an error too, without showing the raw detail', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    listResourceBookings.mockResolvedValue({
      outcome: 'invalid_request',
      detail: 'query.from: input should be a valid datetime',
      raw: null,
    } satisfies ListResourceBookingsResult)
    await renderGrid()

    const alert = screen.getByRole('alert')
    expect(alert).toBeTruthy()
    // Diagnostic text is for the console, not for the user.
    expect(alert.textContent).not.toContain('input should be a valid datetime')
    expect(slot(4, 0).disabled).toBe(true)
  })

  it('retries on demand and recovers', async () => {
    listResourceBookings.mockResolvedValueOnce({ outcome: 'failed', message: 'Nope.' })
    await renderGrid()
    expect(screen.getByTestId('calendar-error')).toBeTruthy()

    resolveWith()
    fireEvent.click(screen.getByTestId('calendar-retry'))
    await waitFor(() => expect(screen.queryByTestId('calendar-error')).toBeNull())
    expect(slot(4, 0).disabled).toBe(false)
  })

  it('does not select when a slot is clicked while the load has failed', async () => {
    listResourceBookings.mockResolvedValue({ outcome: 'failed', message: 'Nope.' })
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

    // Index 20 is 10:00, the booked one. Dragging 17 → 24 must stop at 19.
    fireEvent.pointerDown(slot(4, 17))
    fireEvent.pointerOver(slot(4, 24))
    fireEvent.pointerUp(window)
    expect(selectedIndices(4)).toEqual([17, 18, 19])
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

    // Indices 4 and 5 at 30-minute slots, from midnight, are 02:00 and 02:30.
    const interval = onSelectionChange.mock.calls.at(-1)?.[0] as { start: Date; end: Date }
    expect(interval.start.getHours()).toBe(2)
    expect(interval.start.getMinutes()).toBe(0)
    // Two 30-minute slots, so the range ends at the *end* of the second.
    expect(interval.end.getHours()).toBe(3)
    expect(interval.end.getMinutes()).toBe(0)
  })

  it('still drags across free slots on a day that also has a booking', async () => {
    // The 1.8 regression guard. Booking blocks stopped being
    // `pointer-events-none` so they could be clicked to cancel; if that had cost
    // the grid its drag handlers, this is what would break first.
    const day = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4)
    const start = new Date(day.getFullYear(), day.getMonth(), day.getDate(), 14, 0)
    resolveWith([booking(9, start, new Date(start.getTime() + 60 * 60_000))])
    await renderGrid()

    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerOver(slot(4, 6))
    fireEvent.pointerUp(window)
    expect(selectedIndices(4)).toEqual([4, 5, 6])
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

describe('one click is one session, on the anchored grid', () => {
  /** A uniform week at `sessionMinutes`, every date resolving the same anchor. */
  function scheduleWithSessions(sessionMinutes: number, anchorMinutes: number) {
    return buildWeekSchedule(
      Array.from({ length: DAYS_PER_WEEK }, (_, i) => ({
        date: toDateKey(addDays(MONDAY, i)),
        session_minutes: sessionMinutes,
        opens_at: null,
        closes_at: null,
        coherence_issue: null,
        anchor_minutes: anchorMinutes,
      })),
      SYSTEM_TZ,
    )
  }

  it('proposes exactly one session from a single click', async () => {
    const onSelectionChange = vi.fn()
    await renderGrid({ schedule: scheduleWithSessions(60, 0), onSelectionChange })

    // Index 2 at 60-minute sessions anchored on midnight is 02:00.
    fireEvent.pointerDown(slot(4, 2))
    fireEvent.pointerUp(window)

    expect(selectedIndices(4, { ...THIRTY, sessionMinutes: 60 })).toEqual([2])

    const interval = onSelectionChange.mock.calls.at(-1)?.[0] as { start: Date; end: Date }
    expect(interval.start.getHours()).toBe(2)
    expect(interval.start.getMinutes()).toBe(0)
    expect(interval.end.getHours()).toBe(3)
    expect(interval.end.getMinutes()).toBe(0)
  })

  it('offsets every session by an opening time that does not land on the grid', async () => {
    const onSelectionChange = vi.fn()
    // Opens 09:15 with hour-long sessions: the grid runs 00:15, 01:15, ...
    await renderGrid({ schedule: scheduleWithSessions(60, 9 * 60 + 15), onSelectionChange })

    fireEvent.pointerDown(slot(4, 9))
    fireEvent.pointerUp(window)

    const interval = onSelectionChange.mock.calls.at(-1)?.[0] as { start: Date; end: Date }
    // The first session of the day starts exactly when the venue opens.
    expect(interval.start.getHours()).toBe(9)
    expect(interval.start.getMinutes()).toBe(15)
    expect(interval.end.getHours()).toBe(10)
    expect(interval.end.getMinutes()).toBe(15)
  })

  it('proposes the whole dragged range, in whole sessions', async () => {
    const onSelectionChange = vi.fn()
    await renderGrid({ schedule: scheduleWithSessions(30, 0), onSelectionChange })

    fireEvent.pointerDown(slot(4, 4))
    fireEvent.pointerOver(slot(4, 7))
    fireEvent.pointerUp(window)

    expect(selectedIndices(4)).toEqual([4, 5, 6, 7])

    const interval = onSelectionChange.mock.calls.at(-1)?.[0] as { start: Date; end: Date }
    expect(interval.start.getHours()).toBe(2)
    expect(interval.start.getMinutes()).toBe(0)
    expect(interval.end.getHours()).toBe(4)
    expect(interval.end.getMinutes()).toBe(0)
  })
})
