// @vitest-environment jsdom
/**
 * Component tests for the week grid.
 *
 * Opts into jsdom per-file rather than globally, so the pure-TypeScript suites
 * (the API client, the shape queries, week and selection maths) keep running in
 * the cheaper `node` environment configured in `vite.config.ts`.
 *
 * The claims under test are the ones a reader cannot verify by inspection: that
 * the grid draws exactly the projection it was handed and invents nothing
 * around it — a date the server never sent renders **closed**, a minute no
 * operating interval covers carries no button, and a length the projection did
 * not offer at a start is not constructable by any drag — that navigation
 * *disables* at the horizon rather than silently no-op-ing, and — most
 * importantly — that a failed fetch does not render as a week of free starts,
 * which is the failure mode that would invite a double booking.
 *
 * Every fixture here is a hand-written *server answer*, not a shape document:
 * the projection is computed in `rules/shape/projection.py` and this component
 * only renders it. The three worked examples below (the teacher, the lab, the
 * music room) are the same three `rules/tests/test_shape.py` asserts against
 * the real projection, and their offered starts are copied from what it
 * actually produces — including the ones it drops.
 */

import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Booking, DayProjectionRead, ListResourceBookingsResult } from '../api'
import { CalendarGrid } from './CalendarGrid'
import { closedDayRead, openEveryDay, openWeekRead } from './fixtures'
import { buildWeekProjection, type WeekProjection } from './shape'
import { addDays, DAYS_PER_WEEK, slotTestId, startOfWeek, toDateKey } from './week'
import { SYSTEM_TIME_ZONE } from '../timezone'

const listResourceBookings = vi.hoisted(() => vi.fn())
vi.mock('../api', () => ({ listResourceBookings }))

/** A Wednesday, 14:30 local. Every expectation below is relative to this. */
const NOW = new Date(2026, 6, 22, 14, 30)
/** The Monday of NOW's week. */
const MONDAY = startOfWeek(NOW)

// Every projection below except the dedicated cross-zone `describe` further
// down pins `timeZone` to the environment's own zone, so "the Space's clock"
// and "the environment's clock" are the same thing here — exactly the
// assumption this suite always ran under, preserved on purpose so the
// assertions (`.disabled`, aria-labels built from local wall clock, the
// instants reported upward) still mean what they say.
const SYSTEM_TZ = SYSTEM_TIME_ZONE

/**
 * The default week: every date open all day, starts every 30 minutes, each
 * offering 30/60/90/120 minutes. Answers *every* date rather than a fixed
 * seven, so the navigation suites can page forward without the grid going
 * closed under them.
 */
const OPEN_WEEK = openEveryDay(SYSTEM_TZ)

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'
const RESOURCE_ID = 1

// --- the three worked examples, as the server projects them -----------------
//
// Cross-checked against `rules/tests/test_shape.py`: these are the offered
// starts `project_day` actually produces for the requirements file's own three
// prompts, dropping the candidates that straddle a blackout and never offering
// a residual on its own.

/** The teacher: 18:00-20:00, 20-minute slots, a 19:30-19:40 break. */
function teacherDay(dateKey: string): DayProjectionRead {
  return {
    date: dateKey,
    operating_intervals: [
      { start_minutes: 18 * 60, end_minutes: 20 * 60, allowed_durations_mins: [20] },
    ],
    blackout_intervals: [
      { start_minutes: 19 * 60 + 30, end_minutes: 19 * 60 + 40, reason: 'Break' },
    ],
    // 19:20 is missing: it would run to 19:40 and straddle the break. The
    // 10 minutes left over after it is never offered on its own, because a
    // 10-minute booking is not a duration this block declares.
    offered_starts: [18 * 60, 18 * 60 + 20, 18 * 60 + 40, 19 * 60, 19 * 60 + 40].map(
      (start_minutes) => ({ start_minutes, durations_mins: [20] }),
    ),
    bookable: true,
  }
}

/** The lab: 08:00-17:00, 30-minute slots, three 20-minute cooldowns. */
function labDay(dateKey: string): DayProjectionRead {
  const cooldowns = [10 * 60, 13 * 60, 15 * 60]
  const offered: DayProjectionRead['offered_starts'] = []
  for (let start = 8 * 60; start + 30 <= 17 * 60; start += 30) {
    // Each cooldown straddles precisely the slot beginning at its own start
    // time — the 08:00-anchored grid lands exactly on 10:00, 13:00 and 15:00.
    if (cooldowns.includes(start)) continue
    offered.push({ start_minutes: start, durations_mins: [30] })
  }
  return {
    date: dateKey,
    operating_intervals: [
      { start_minutes: 8 * 60, end_minutes: 17 * 60, allowed_durations_mins: [30] },
    ],
    blackout_intervals: cooldowns.map((start_minutes) => ({
      start_minutes,
      end_minutes: start_minutes + 20,
      reason: 'Cooldown',
    })),
    offered_starts: offered,
    bookable: true,
  }
}

/** The music room: 08:00-14:00 at [60], 14:00-22:00 at [60, 120]. Two blocks, never merged. */
function musicRoomDay(dateKey: string): DayProjectionRead {
  const offered: DayProjectionRead['offered_starts'] = []
  for (let start = 8 * 60; start + 60 <= 14 * 60; start += 60) {
    offered.push({ start_minutes: start, durations_mins: [60] })
  }
  for (let start = 14 * 60; start + 60 <= 22 * 60; start += 60) {
    // 120 only where it still ends inside the evening block: 21:00 offers an
    // hour and nothing longer.
    const durations = start + 120 <= 22 * 60 ? [60, 120] : [60]
    offered.push({ start_minutes: start, durations_mins: durations })
  }
  return {
    date: dateKey,
    operating_intervals: [
      { start_minutes: 8 * 60, end_minutes: 14 * 60, allowed_durations_mins: [60] },
      { start_minutes: 14 * 60, end_minutes: 22 * 60, allowed_durations_mins: [60, 120] },
    ],
    blackout_intervals: [],
    offered_starts: offered,
    bookable: true,
  }
}

/** A week in which only `dayOffset` is projected at all, from `read`. */
function weekWith(dayOffset: number, read: (dateKey: string) => DayProjectionRead): WeekProjection {
  return buildWeekProjection([read(toDateKey(dayFor(dayOffset)))], SYSTEM_TZ)
}

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
 *
 * `week` defaults to the open projection rather than to the component's own
 * fail-closed default, so a test that is about something else does not have to
 * restate a shape. The fail-closed default itself is asserted directly, with
 * no harness, in "a caller that hands it no projection".
 */
function GridHarness({
  initialWeekStart = MONDAY,
  week = OPEN_WEEK,
  onWeekChange,
  ...rest
}: GridHarnessProps) {
  const [weekStart, setWeekStart] = useState(initialWeekStart)
  return (
    <CalendarGrid
      publicId={PUBLIC_ID}
      resourceId={RESOURCE_ID}
      now={NOW}
      week={week}
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

/** The calendar-date carrier `dayOffset` days after Monday. */
function dayFor(dayOffset: number): Date {
  return addDays(MONDAY, dayOffset)
}

/** The offered-start button at `startMinutes` on a day offset from Monday. */
function slot(dayOffset: number, startMinutes: number): HTMLButtonElement {
  return screen.getByTestId(slotTestId(dayFor(dayOffset), startMinutes)) as HTMLButtonElement
}

/** The same button, or `null` when the projection offered no start there. */
function querySlot(dayOffset: number, startMinutes: number): HTMLButtonElement | null {
  return screen.queryByTestId(slotTestId(dayFor(dayOffset), startMinutes)) as HTMLButtonElement | null
}

/** Every offered start rendered on `dayOffset`, in minutes, ascending. */
function offeredStarts(dayOffset: number): number[] {
  return startButtons(dayOffset).map(startMinutesOf)
}

/** The starts on `dayOffset` currently painted as selected, in minutes, ascending. */
function selectedStarts(dayOffset: number): number[] {
  return startButtons(dayOffset)
    .filter((button) => button.dataset.selected === 'true')
    .map(startMinutesOf)
}

function startButtons(dayOffset: number): HTMLButtonElement[] {
  const column = screen.getByTestId(`calendar-day-column-${toDateKey(dayFor(dayOffset))}`)
  // Scoped to the start buttons: a booking block in the same column also
  // carries `data-selected`, and it is a different kind of selection.
  return Array.from(column.querySelectorAll<HTMLButtonElement>('button[data-testid^="slot-"]'))
}

function startMinutesOf(button: HTMLButtonElement): number {
  return Number(button.dataset.testid?.split('-').pop())
}

beforeEach(() => {
  listResourceBookings.mockReset()
  resolveWith()
})

afterEach(cleanup)

describe('the grid is drawn from the projection', () => {
  it('renders one button per offered start and nothing between them', async () => {
    await renderGrid({ week: weekWith(4, teacherDay) })

    expect(offeredStarts(4)).toEqual([1080, 1100, 1120, 1140, 1180])
    // 19:20 is not offered — the projection dropped it, and the grid has no
    // arithmetic of its own that could put it back.
    expect(querySlot(4, 1160)).toBeNull()
    // Nor is any minute outside the operating block: 17:40 is closed time,
    // which is a painted region with no button in it at all.
    expect(querySlot(4, 1060)).toBeNull()
  })

  it('reports each column’s own count of offered starts', async () => {
    await renderGrid({ week: weekWith(4, teacherDay) })

    const column = screen.getByTestId(`calendar-day-column-${toDateKey(dayFor(4))}`)
    expect(column.dataset.offeredStarts).toBe('5')
  })

  it('renders seven day columns whatever any of them offers', async () => {
    await renderGrid({ week: weekWith(4, teacherDay) })

    for (let offset = 0; offset < DAYS_PER_WEEK; offset += 1) {
      expect(screen.getByTestId(`calendar-day-${toDateKey(dayFor(offset))}`)).toBeTruthy()
    }
  })

  it('labels a start with the local wall clock it begins at', async () => {
    await renderGrid({ week: weekWith(4, teacherDay) })

    expect(slot(4, 1080).getAttribute('aria-label')).toBe(`${toDateKey(dayFor(4))} 18:00`)
  })

  it('renders a date the projection never sent as closed, offering nothing', async () => {
    // The fail-closed inversion: the grid used to fall back to "the whole day,
    // default slot size" for a date it had no answer for, which is the grid
    // inventing availability the server never asserted.
    await renderGrid({ week: weekWith(4, teacherDay) })

    expect(offeredStarts(3)).toEqual([])
    expect(
      screen.getByTestId(`calendar-day-column-${toDateKey(dayFor(3))}`).dataset.offeredStarts,
    ).toBe('0')
  })

  it('renders a date the shape covers but offers nothing on as closed too', async () => {
    const week = buildWeekProjection(
      [teacherDay(toDateKey(dayFor(4))), closedDayRead(toDateKey(dayFor(3)))],
      SYSTEM_TZ,
    )
    await renderGrid({ week })

    expect(offeredStarts(3)).toEqual([])
    expect(offeredStarts(4).length).toBe(5)
  })
})

describe('a caller that hands it no projection', () => {
  it('renders every date closed rather than a permissively open week', async () => {
    // No `week` prop at all — a caller whose Space shape has not resolved yet.
    // The default is `CLOSED_WEEK`, never an open one: offering a start on a
    // shape nobody has read is exactly the defect this stream closes.
    render(
      <CalendarGrid publicId={PUBLIC_ID} resourceId={RESOURCE_ID} now={NOW} weekStart={MONDAY} />,
    )
    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())

    for (let offset = 0; offset < DAYS_PER_WEEK; offset += 1) {
      expect(offeredStarts(offset)).toEqual([])
    }
    expect(screen.getByTestId('calendar-grid')).toBeTruthy()
  })
})

describe('the teacher: 20-minute slots from 18:00, a break at 19:30', () => {
  it('draws the break with its own reason, so the hole in the day is legible', async () => {
    await renderGrid({ week: weekWith(4, teacherDay) })

    const blackout = screen.getByTestId(`blackout-${toDateKey(dayFor(4))}-1170`)
    expect(blackout.textContent).toBe('Break')
    expect(blackout.getAttribute('title')).toBe('Break')
  })

  it('takes the offered duration from a click, and only that', async () => {
    const onSelectionChange = vi.fn()
    await renderGrid({ week: weekWith(4, teacherDay), onSelectionChange })

    fireEvent.pointerDown(slot(4, 1080))
    fireEvent.pointerUp(window)

    expect(screen.getByTestId('calendar-selection').textContent).toContain('Selected 20 minutes')
    const interval = onSelectionChange.mock.calls.at(-1)?.[0] as { start: Date; end: Date }
    expect(interval.start.getHours()).toBe(18)
    expect(interval.end.getHours()).toBe(18)
    expect(interval.end.getMinutes()).toBe(20)
  })

  it('cannot be dragged across the break', async () => {
    await renderGrid({ week: weekWith(4, teacherDay) })

    // 19:00 to the far side of the break. 20 minutes is the only duration
    // offered at 19:00, so the drag stops at 19:20 — the minutes the break
    // occupies are not reachable by stretching a selection into them.
    fireEvent.pointerDown(slot(4, 1140))
    fireEvent.pointerOver(slot(4, 1180))
    fireEvent.pointerUp(window)

    expect(selectedStarts(4)).toEqual([1140])
    expect(screen.getByTestId('calendar-selection').textContent).toContain('Selected 20 minutes')
  })
})

describe('the lab: 30-minute slots, three cooldowns', () => {
  it('greys each cooldown with its reason', async () => {
    await renderGrid({ week: weekWith(4, labDay) })

    const dateKey = toDateKey(dayFor(4))
    for (const start of [600, 780, 900]) {
      expect(screen.getByTestId(`blackout-${dateKey}-${start}`).textContent).toBe('Cooldown')
    }
  })

  it('offers no start that would run into a cooldown, and no residual after one', async () => {
    await renderGrid({ week: weekWith(4, labDay) })

    for (const start of [600, 780, 900]) {
      // The 30-minute slot beginning at the cooldown would straddle it.
      expect(querySlot(4, start)).toBeNull()
      // The slot before it ends exactly as the cooldown begins, and the one
      // after begins once it is over — both are ordinary offered starts.
      expect(slot(4, start - 30)).toBeTruthy()
      expect(slot(4, start + 30)).toBeTruthy()
      // The 10 minutes left over after the cooldown is never offered alone:
      // it is not a duration the block declares, so it was never a candidate.
      expect(querySlot(4, start + 20)).toBeNull()
    }
  })

  it('never paints a start button over a cooldown', async () => {
    await renderGrid({ week: weekWith(4, labDay) })

    const dateKey = toDateKey(dayFor(4))
    for (const start of [600, 780, 900]) {
      const blackout = screen.getByTestId(`blackout-${dateKey}-${start}`)
      const top = parseFloat(blackout.style.top)
      const bottom = top + parseFloat(blackout.style.height)
      for (const button of startButtons(4)) {
        const buttonTop = parseFloat(button.style.top)
        const buttonBottom = buttonTop + parseFloat(button.style.height)
        expect(buttonTop < bottom && top < buttonBottom).toBe(false)
      }
    }
  })
})

describe('the music room: an hour in the morning, one or two in the evening', () => {
  it('stretches an evening drag to two hours', async () => {
    const onSelectionChange = vi.fn()
    await renderGrid({ week: weekWith(4, musicRoomDay), onSelectionChange })

    fireEvent.pointerDown(slot(4, 14 * 60))
    fireEvent.pointerOver(slot(4, 15 * 60))
    fireEvent.pointerUp(window)

    expect(screen.getByTestId('calendar-selection').textContent).toContain('Selected 120 minutes')
    // Both covered starts paint as selected — the selection is a span, not a
    // single button.
    expect(selectedStarts(4)).toEqual([14 * 60, 15 * 60])
    const interval = onSelectionChange.mock.calls.at(-1)?.[0] as { start: Date; end: Date }
    expect(interval.start.getHours()).toBe(14)
    expect(interval.end.getHours()).toBe(16)
  })

  it('will not stretch a morning drag past the hour the morning block offers', async () => {
    await renderGrid({ week: weekWith(4, musicRoomDay) })

    fireEvent.pointerDown(slot(4, 9 * 60))
    fireEvent.pointerOver(slot(4, 11 * 60))
    fireEvent.pointerUp(window)

    // 60 is the only duration the morning block declares, so a drag over two
    // further hours still proposes one. The length is not constructable, which
    // is the structural half of the reported "the grid builds a selection the
    // server refuses" defect.
    expect(selectedStarts(4)).toEqual([9 * 60])
    expect(screen.getByTestId('calendar-selection').textContent).toContain('Selected 60 minutes')
  })

  it('takes the smallest offered duration from a plain click in the evening', async () => {
    await renderGrid({ week: weekWith(4, musicRoomDay) })

    fireEvent.pointerDown(slot(4, 14 * 60))
    fireEvent.pointerUp(window)

    expect(screen.getByTestId('calendar-selection').textContent).toContain('Selected 60 minutes')
  })

  it('keeps every start clickable where two blocks put them closer together', async () => {
    // The morning block's last hour starts at 13:00 and the evening block's
    // grid begins at 14:00 — an hour apart here, but a block's own click unit
    // is what caps a button's height, so no start can paint over its
    // neighbour whatever the two anchors are.
    await renderGrid({ week: weekWith(4, musicRoomDay) })

    const buttons = startButtons(4)
    for (let i = 0; i < buttons.length - 1; i += 1) {
      const bottom = parseFloat(buttons[i].style.top) + parseFloat(buttons[i].style.height)
      expect(bottom).toBeLessThanOrEqual(parseFloat(buttons[i + 1].style.top) + 0.001)
    }
  })
})

describe('a blackout inside a block that offers a longer duration', () => {
  // 08:00-14:00 at {60, 120} with a 10:00-10:30 break: the projection offers
  // 120 at 08:00 (ending exactly as the break begins) but not at 09:00, and
  // offers nothing at all at 10:00.
  const dateKey = toDateKey(addDays(MONDAY, 4))
  const week = buildWeekProjection(
    [
      {
        date: dateKey,
        operating_intervals: [
          { start_minutes: 480, end_minutes: 840, allowed_durations_mins: [60, 120] },
        ],
        blackout_intervals: [{ start_minutes: 600, end_minutes: 630, reason: 'Break' }],
        offered_starts: [
          { start_minutes: 480, durations_mins: [60, 120] },
          { start_minutes: 540, durations_mins: [60] },
          { start_minutes: 660, durations_mins: [60, 120] },
          { start_minutes: 720, durations_mins: [60] },
        ],
        bookable: true,
      },
    ],
    SYSTEM_TZ,
  )

  it('truncates a drag at the break rather than spanning it', async () => {
    await renderGrid({ week })

    // Dragging 08:00 → 11:00. The widest duration offered at 08:00 is 120,
    // which ends exactly as the break begins; nothing longer is offered, so
    // the selection stops there instead of swallowing the break.
    fireEvent.pointerDown(slot(4, 480))
    fireEvent.pointerOver(slot(4, 660))
    fireEvent.pointerUp(window)

    expect(screen.getByTestId('calendar-selection').textContent).toContain('Selected 120 minutes')
    expect(selectedStarts(4)).toEqual([480, 540])
  })

  it('offers only the hour at the start the break truncates', async () => {
    await renderGrid({ week })

    fireEvent.pointerDown(slot(4, 540))
    fireEvent.pointerOver(slot(4, 720))
    fireEvent.pointerUp(window)

    expect(screen.getByTestId('calendar-selection').textContent).toContain('Selected 60 minutes')
  })
})

describe('a window past local midnight', () => {
  // Representable in the shape and deliberately not drawn — see
  // `CalendarGrid.tsx`'s "clamped, never wrapped" section and
  // `ops/done/stream-7/passed-midnight.md`.
  const dateKey = toDateKey(addDays(MONDAY, 4))
  const week = buildWeekProjection(
    [
      {
        date: dateKey,
        operating_intervals: [
          { start_minutes: 1320, end_minutes: 1560, allowed_durations_mins: [60] },
        ],
        blackout_intervals: [],
        offered_starts: [1320, 1380, 1440, 1500].map((start_minutes) => ({
          start_minutes,
          durations_mins: [60],
        })),
        bookable: true,
      },
    ],
    SYSTEM_TZ,
  )

  it('draws the starts before midnight and none at or past it', async () => {
    await renderGrid({ week })

    expect(offeredStarts(4)).toEqual([1320, 1380])
    expect(querySlot(4, 1440)).toBeNull()
    expect(querySlot(4, 1500)).toBeNull()
  })

  it('clips the operating region at the day boundary rather than overflowing it', async () => {
    await renderGrid({ week })

    const column = screen.getByTestId(`calendar-day-column-${dateKey}`)
    const height = parseFloat(column.style.height)
    for (const region of Array.from(column.querySelectorAll<HTMLElement>('div[style]'))) {
      const bottom = parseFloat(region.style.top) + parseFloat(region.style.height)
      if (Number.isFinite(bottom)) expect(bottom).toBeLessThanOrEqual(height + 0.001)
    }
  })
})

describe('the DEFERRED.md item 19 repro: a Space in one zone, a viewer in another', () => {
  // Exactly the repro recorded in the deferred entry: a Space at
  // `Europe/Berlin`, open 13:00-22:00. August 3 2026 is a Monday, and Berlin
  // is on CEST (UTC+2) that week, so the Space's 13:00 opening is 11:00Z —
  // never 10:00Z, which is what sending the *viewer's* own zone (the item 19
  // repro used Asia/Jerusalem, UTC+3) would have produced.
  const AUGUST_MONDAY = new Date(2026, 7, 3)
  const BERLIN_WEEK = buildWeekProjection(
    openWeekRead(AUGUST_MONDAY, {
      stepMinutes: 30,
      durationsMins: [30],
      openFromMinutes: 13 * 60,
      openUntilMinutes: 22 * 60,
    }),
    'Europe/Berlin',
  )

  it('offers the 13:00 start and draws nothing before it', async () => {
    await renderGrid({ week: BERLIN_WEEK, initialWeekStart: AUGUST_MONDAY })

    const open = screen.getByTestId(slotTestId(AUGUST_MONDAY, 13 * 60)) as HTMLButtonElement
    expect(open.dataset.blocked).toBeUndefined()
    expect(open.disabled).toBe(false)
    // 12:30 is closed time: painted, with no button in it. The grid draws a
    // real boundary at 13:00, not an accidentally-permissive one.
    expect(screen.queryByTestId(slotTestId(AUGUST_MONDAY, 12 * 60 + 30))).toBeNull()
  })

  it('selects the exact instant the backend accepts — 11:00Z, not 10:00Z', async () => {
    const onSelectionChange = vi.fn()
    await renderGrid({
      week: BERLIN_WEEK,
      initialWeekStart: AUGUST_MONDAY,
      onSelectionChange,
    })

    fireEvent.pointerDown(screen.getByTestId(slotTestId(AUGUST_MONDAY, 13 * 60)))
    fireEvent.pointerUp(window)

    const interval = onSelectionChange.mock.calls.at(-1)?.[0] as { start: Date; end: Date }
    expect(interval.start.toISOString()).toBe('2026-08-03T11:00:00.000Z')
  })
})

describe('past starts', () => {
  it('render disabled rather than hidden, so the week does not reflow', async () => {
    await renderGrid()
    // Monday 00:00 is three days behind NOW but still present in the grid.
    const monday = slot(0, 0)
    expect(monday).toBeTruthy()
    expect(monday.disabled).toBe(true)
    expect(monday.dataset.blocked).toBe('past')
  })

  it('disables earlier starts on today but not later ones', async () => {
    await renderGrid()
    // NOW is 14:30 on Wednesday (day offset 2). Asserting both directions makes
    // this non-vacuous: a component that disabled everything, or nothing, fails
    // one half.
    expect(slot(2, 14 * 60).disabled).toBe(true)
    expect(slot(2, 14 * 60).dataset.blocked).toBe('past')
    expect(slot(2, 15 * 60).disabled).toBe(false)
    expect(slot(2, 15 * 60).dataset.blocked).toBeUndefined()
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
    render(
      <CalendarGrid
        publicId={PUBLIC_ID}
        resourceId={RESOURCE_ID}
        now={NOW}
        week={OPEN_WEEK}
        weekStart={MONDAY}
        onWeekChange={onWeekChange}
      />,
    )
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

  it('disables the starts past the horizon on the final reachable week', async () => {
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
    const at = (day: Date, startMinutes: number) =>
      (screen.getByTestId(slotTestId(day, startMinutes)) as HTMLButtonElement).dataset.blocked

    expect(at(horizonDay, 14 * 60)).toBeUndefined()
    expect(at(horizonDay, 15 * 60)).toBe('beyond-horizon')
    // The day before is bookable right up to the end of the day — the
    // negative control proving the assertion above is not just "everything
    // late is blocked".
    expect(at(new Date(2026, 8, 19), 23 * 60 + 30)).toBeUndefined()
  })
})

describe('existing bookings', () => {
  it('renders a block per booking and disables the starts it covers', async () => {
    const start = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4, 9, 0)
    const end = new Date(start.getTime() + 90 * 60_000)
    resolveWith([booking(7, start, end)])
    await renderGrid()

    expect(screen.getByTestId('booking-7')).toBeTruthy()
    // 09:00–10:30 covers the starts at 09:00, 09:30 and 10:00.
    expect(slot(4, 540).dataset.blocked).toBe('booked')
    expect(slot(4, 570).dataset.blocked).toBe('booked')
    expect(slot(4, 600).dataset.blocked).toBe('booked')
    // Half-open: the start at the booking's own end is free, and so is the
    // one whose click unit ends exactly as the booking begins.
    expect(slot(4, 630).dataset.blocked).toBeUndefined()
    expect(slot(4, 510).dataset.blocked).toBeUndefined()
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

  it("renders the caller's own booking as before: indigo, with no \"someone else\" copy", async () => {
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

describe('showBookings: false — the seam the chat preview reuses this through', () => {
  it('issues no booking request and draws no booking layer', async () => {
    const start = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4, 9, 0)
    resolveWith([booking(12, start, new Date(start.getTime() + 60 * 60_000))])
    await renderGrid({ showBookings: false })

    expect(listResourceBookings).not.toHaveBeenCalled()
    expect(screen.queryByTestId('booking-12')).toBeNull()
  })

  it('still draws the shape, and its starts are selectable', async () => {
    await renderGrid({ week: weekWith(4, teacherDay), showBookings: false })

    expect(offeredStarts(4)).toEqual([1080, 1100, 1120, 1140, 1180])
    expect(slot(4, 1080).disabled).toBe(false)
  })
})

describe('selecting a booking to cancel it', () => {
  /** A one-hour booking on Friday at 14:00. */
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

    fireEvent.pointerDown(slot(4, 120))
    fireEvent.pointerUp(window)
    expect(selectedStarts(4)).toEqual([120])

    fireEvent.click(screen.getByTestId('booking-11'))
    expect(selectedStarts(4)).toEqual([])
    expect(screen.getByTestId('booking-11').dataset.selected).toBe('true')
  })

  it('is retracted in turn when a drag starts on a free start', async () => {
    const onBookingSelect = vi.fn()
    withBooking()
    await renderGrid({ onBookingSelect })

    fireEvent.click(screen.getByTestId('booking-11'))
    fireEvent.pointerDown(slot(4, 120))
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
    // gone, and the freed starts must be selectable again.
    resolveWith([])
    await act(async () => {
      rerender(
        <CalendarGrid
          publicId={PUBLIC_ID}
          resourceId={RESOURCE_ID}
          now={NOW}
          week={OPEN_WEEK}
          weekStart={MONDAY}
          onBookingSelect={onBookingSelect}
          refreshToken={1}
        />,
      )
    })
    await waitFor(() => expect(screen.queryByTestId('booking-11')).toBeNull())

    expect(onBookingSelect.mock.calls.at(-1)?.[0]).toBeNull()
    expect(slot(4, 14 * 60).disabled).toBe(false)
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
          week={OPEN_WEEK}
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
    // Pinning the mechanism is the only guard available; the Playwright suite
    // is what exercises the real thing.
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
        week={OPEN_WEEK}
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
        week={OPEN_WEEK}
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
      <CalendarGrid
        publicId={PUBLIC_ID}
        resourceId={RESOURCE_ID}
        now={NOW}
        week={OPEN_WEEK}
        weekStart={MONDAY}
      />,
    )
    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
    fireEvent.pointerDown(slot(4, 120))
    fireEvent.pointerUp(window)
    expect(screen.getByTestId('calendar-selection')).toBeTruthy()

    rerender(
      <CalendarGrid
        publicId={PUBLIC_ID}
        resourceId={RESOURCE_ID}
        now={NOW}
        week={OPEN_WEEK}
        weekStart={addDays(MONDAY, DAYS_PER_WEEK)}
      />,
    )
    expect(screen.queryByTestId('calendar-selection')).toBeNull()
  })

  it('never covers a start that was selectable', async () => {
    // The invariant that makes clickable blocks safe. A block sits on top of
    // the offered-start buttons and intercepts their pointer events, which
    // would be a real hazard if it could shadow a start the user was allowed
    // to drag through. It cannot: the block is laid out from the same interval
    // that makes every start it touches `blocked === 'booked'`.
    //
    // jsdom has no layout and `fireEvent` dispatches straight at its target, so
    // hit-testing itself is unobservable here. Asserting the geometric
    // invariant is what actually holds the guarantee up.
    withBooking()
    await renderGrid()

    const block = screen.getByTestId('booking-11')
    const top = parseFloat(block.style.top)
    const bottom = top + parseFloat(block.style.height)

    let covered = 0
    for (const button of startButtons(4)) {
      const buttonTop = parseFloat(button.style.top)
      const buttonBottom = buttonTop + parseFloat(button.style.height)
      if (buttonTop < bottom && top < buttonBottom) {
        covered += 1
        expect(button.disabled).toBe(true)
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
    // bookable. An empty grid of *enabled* starts is the double-booking trap.
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
      // Every start is unavailable, exactly as an ordinary network failure
      // would leave it — the grid has no trustworthy answer about what is
      // booked.
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

  it('does not select when a start is clicked while the load has failed', async () => {
    listResourceBookings.mockResolvedValue({ outcome: 'failed', message: 'Nope.' })
    await renderGrid()
    fireEvent.pointerDown(slot(4, 120))
    expect(screen.queryByTestId('calendar-selection')).toBeNull()
  })
})

describe('selection', () => {
  it('takes the smallest offered duration from a click', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 120))
    fireEvent.pointerUp(window)
    expect(selectedStarts(4)).toEqual([120])
    expect(screen.getByTestId('calendar-selection').textContent).toContain('Selected 30 minutes')
  })

  it('takes the largest offered duration that fits when dragged downward', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 120))
    fireEvent.pointerOver(slot(4, 210))
    fireEvent.pointerUp(window)
    // The drag head's own click unit ends at 04:00, and 120 is the widest
    // duration offered at 02:00 that ends no later — so every start it covers
    // paints as selected.
    expect(selectedStarts(4)).toEqual([120, 150, 180, 210])
    expect(screen.getByTestId('calendar-selection').textContent).toContain('Selected 120 minutes')
  })

  it('resolves a backwards drag from the earlier start, exactly as a forward one', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 240))
    fireEvent.pointerOver(slot(4, 150))
    fireEvent.pointerUp(window)
    expect(selectedStarts(4)).toEqual([150, 180, 210, 240])
  })

  it('stops short of a booked start instead of selecting through it', async () => {
    const day = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4)
    const start = new Date(day.getFullYear(), day.getMonth(), day.getDate(), 10, 0)
    resolveWith([booking(3, start, new Date(start.getTime() + 30 * 60_000))])
    await renderGrid()

    // 10:00 is booked. Dragging 08:30 → 12:00 takes the widest duration whose
    // whole span is still free: 90 minutes, ending exactly as the booking
    // begins.
    fireEvent.pointerDown(slot(4, 510))
    fireEvent.pointerOver(slot(4, 720))
    fireEvent.pointerUp(window)
    expect(selectedStarts(4)).toEqual([510, 540, 570])
  })

  it('ignores a drag onto another day column', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 120))
    fireEvent.pointerOver(slot(5, 270))
    fireEvent.pointerUp(window)
    expect(selectedStarts(4)).toEqual([120])
    expect(selectedStarts(5)).toEqual([])
  })

  it('does not extend after the pointer is released', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 120))
    fireEvent.pointerUp(window)
    fireEvent.pointerOver(slot(4, 270))
    expect(selectedStarts(4)).toEqual([120])
  })

  it('reports the selected interval as wall-clock times', async () => {
    const onSelectionChange = vi.fn()
    await renderGrid({ onSelectionChange })
    fireEvent.pointerDown(slot(4, 120))
    fireEvent.pointerOver(slot(4, 150))
    fireEvent.pointerUp(window)

    const interval = onSelectionChange.mock.calls.at(-1)?.[0] as { start: Date; end: Date }
    expect(interval.start.getHours()).toBe(2)
    expect(interval.start.getMinutes()).toBe(0)
    // 60 minutes: the widest duration offered at 02:00 that ends no later than
    // the head's own 03:00.
    expect(interval.end.getHours()).toBe(3)
    expect(interval.end.getMinutes()).toBe(0)
  })

  it('still drags across free starts on a day that also has a booking', async () => {
    // The 1.8 regression guard. Booking blocks stopped being
    // `pointer-events-none` so they could be clicked to cancel; if that had cost
    // the grid its drag handlers, this is what would break first.
    const day = new Date(MONDAY.getFullYear(), MONDAY.getMonth(), MONDAY.getDate() + 4)
    const start = new Date(day.getFullYear(), day.getMonth(), day.getDate(), 14, 0)
    resolveWith([booking(9, start, new Date(start.getTime() + 60 * 60_000))])
    await renderGrid()

    fireEvent.pointerDown(slot(4, 120))
    fireEvent.pointerOver(slot(4, 180))
    fireEvent.pointerUp(window)
    expect(selectedStarts(4)).toEqual([120, 150, 180])
  })

  it('clears the selection when the week changes', async () => {
    await renderGrid()
    fireEvent.pointerDown(slot(4, 120))
    fireEvent.pointerUp(window)
    expect(screen.getByTestId('calendar-selection')).toBeTruthy()

    await act(async () => {
      fireEvent.click(screen.getByTestId('calendar-next-week'))
    })
    await waitFor(() => expect(screen.queryByTestId('calendar-selection')).toBeNull())
  })
})
