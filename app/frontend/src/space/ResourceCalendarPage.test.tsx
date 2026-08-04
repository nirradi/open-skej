// @vitest-environment jsdom
/**
 * Tests for `/s/{public_id}/resources/{resource_id}`, the per-Resource
 * calendar.
 *
 * This component's own job is narrow: read the two route params, hand them
 * down to `CalendarGrid` / `BookingPanel` / `CancelPanel` so every request
 * those make is scoped to the right Space and Resource, wire the shared
 * refresh-token pattern between them, and show a back link plus some header
 * context. The grid/panel behaviour itself is covered in their own suites;
 * `App.test.tsx` covers the round trip between them.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryRouter, MemoryRouter, Route, RouterProvider, Routes } from 'react-router-dom'

import { getSpace, getSpaceSchedule, listResourceBookings, listResources } from '../api'
import type { ApiOk, DayScheduleRead, Resource, Space } from '../api'
import {
  addDays,
  dayBounds,
  DAYS_PER_WEEK,
  horizonEnd,
  slotTestId,
  startOfWeek,
  toDateKey,
} from '../calendar/week'
import { zonedCalendarDate } from '../timezone'
import { ResourceCalendarPage } from './ResourceCalendarPage'

vi.mock('../api', () => ({
  listResourceBookings: vi.fn(),
  createResourceBooking: vi.fn(),
  cancelResourceBooking: vi.fn(),
  getSpace: vi.fn(),
  getSpaceSchedule: vi.fn(),
  listResources: vi.fn(),
}))

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'
const RESOURCE_ID = 7

const SPACE: Space = {
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
}

const RESOURCE: Resource = {
  id: RESOURCE_ID,
  name: 'Court 1',
  created_at: '2026-07-01T00:00:00Z',
  archived_at: null,
}

const OTHER_RESOURCE: Resource = {
  id: RESOURCE_ID + 1,
  name: 'Court 2',
  created_at: '2026-07-01T00:00:00Z',
  archived_at: null,
}

function ok<T>(data: T): ApiOk<T> {
  return { outcome: 'ok', data }
}

/**
 * `DayScheduleRead[]` for every date in `[from, from+days)`, all identical —
 * the uniform-week shape most of this suite's tests want from
 * `getSpaceSchedule`, built from whatever window the page actually requests
 * rather than a hardcoded one, so it stays correct whichever week a test
 * navigates to.
 */
function uniformScheduleEntries(
  from: Date,
  days: number,
  overrides: Partial<Omit<DayScheduleRead, 'date'>> = {},
): DayScheduleRead[] {
  return Array.from({ length: days }, (_, i) => ({
    date: toDateKey(addDays(from, i)),
    slot_minutes: null,
    opens_at: null,
    closes_at: null,
    coherence_issue: null,
    ...overrides,
  }))
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/s/:publicId" element={<p data-testid="space-page">Space</p>} />
        <Route path="/s/:publicId/resources/:resourceId" element={<ResourceCalendarPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

const ROUTES = [
  { path: '/s/:publicId', element: <p data-testid="space-page">Space</p> },
  { path: '/s/:publicId/resources/:resourceId', element: <ResourceCalendarPage /> },
]

/** Renders through a data router, so a test can drive Back with `router.navigate(-1)`. */
function renderRouterAt(path: string) {
  const router = createMemoryRouter(ROUTES, { initialEntries: [path] })
  const view = render(<RouterProvider router={router} />)
  return { router, ...view }
}

/** The `from` Date of `listResourceBookings`'s `n`th call (0-indexed). */
function requestedWeekStart(n: number): Date {
  return vi.mocked(listResourceBookings).mock.calls[n][2] as Date
}

/**
 * Waits for the header fetch to settle, then returns the `from` Date of the
 * *last* `listResourceBookings` call so far.
 *
 * Two calls, not one, is the correct shape while the header is pending:
 * `CalendarGrid` fetches immediately on its own placeholder `timeZone`
 * (`calendarConfig`'s module default) rather than waiting on the header, per
 * its own docblock — responsiveness over waterfalling. Once `getSpace`
 * resolves, `config` (and therefore the fetch window `dayBounds` resolves
 * through) can genuinely change, firing a second, corrected fetch. The
 * placeholder and the Space's real zone coincide when the environment's own
 * zone happens to already be the Space's (nothing to correct, one call); they
 * differ on any other machine, which is exactly what this suite's `SPACE`
 * fixture (`timezone: 'UTC'`) is not guaranteed to match. Reading the *last*
 * call after the header settles is what stays correct either way, instead of
 * assuming call `0` is always the final answer.
 */
async function waitForSettledWeekStart(): Promise<Date> {
  await waitFor(() =>
    expect(screen.getByTestId('resource-calendar-heading').textContent).toBe(
      `${SPACE.name} — ${RESOURCE.name}`,
    ),
  )
  const calls = vi.mocked(listResourceBookings).mock.calls
  return calls[calls.length - 1][2] as Date
}

beforeEach(() => {
  vi.mocked(listResourceBookings).mockResolvedValue(ok([]))
  vi.mocked(getSpace).mockResolvedValue(ok(SPACE))
  // Unrestricted by default — no hours bound, the shipped default slot size —
  // matching the pre-6.9 default this suite's `SPACE` fixture already used
  // (`opens_at`/`closes_at`/`slot_minutes` all `null`).
  vi.mocked(getSpaceSchedule).mockImplementation(async (_publicId, from, days) =>
    ok(uniformScheduleEntries(from, days)),
  )
  // Two active Resources by default, so the generic header tests exercise
  // the "there is a real picker to go back to" case; the back-link tests
  // below override this to one Resource for the opposite case.
  vi.mocked(listResources).mockResolvedValue(ok([RESOURCE, OTHER_RESOURCE]))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('the header', () => {
  it('shows a generic heading before the Space/Resource names resolve', () => {
    vi.mocked(getSpace).mockReturnValue(new Promise(() => {}))
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    expect(screen.getByTestId('resource-calendar-heading').textContent).toBe('Calendar')
  })

  it('names the Space and the Resource once both resolve', async () => {
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    await waitFor(() =>
      expect(screen.getByTestId('resource-calendar-heading').textContent).toBe(
        'Tennis Court — Court 1',
      ),
    )
  })

  it('does not block the calendar on the header fetch failing', async () => {
    vi.mocked(getSpace).mockResolvedValue({ outcome: 'failed', message: 'Nope.' })
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    // The calendar's own request is independent of the header's, so it still
    // renders and settles even though the header stays on its fallback text.
    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
    expect(screen.getByTestId('calendar-grid')).toBeTruthy()
    expect(screen.getByTestId('resource-calendar-heading').textContent).toBe('Calendar')
  })

  it('links back to the Space while there is a real picker to return to', () => {
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    const back = screen.getByTestId('resource-calendar-back')
    expect(back.getAttribute('href')).toBe(`/s/${PUBLIC_ID}`)
  })

  it('shows the back link before the header has resolved', () => {
    vi.mocked(getSpace).mockReturnValue(new Promise(() => {}))
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    expect(screen.getByTestId('resource-calendar-back')).toBeTruthy()
  })

  it('hides the back link once this is confirmed the Space\'s only active Resource', async () => {
    vi.mocked(listResources).mockResolvedValue(ok([RESOURCE]))
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    await waitFor(() =>
      expect(screen.getByTestId('resource-calendar-heading').textContent).toBe(
        'Tennis Court — Court 1',
      ),
    )
    expect(screen.queryByTestId('resource-calendar-back')).toBeNull()
  })
})

describe('scoping the calendar and the panels', () => {
  it('requests bookings scoped to the Space and Resource from the route', async () => {
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    await waitFor(() => expect(listResourceBookings).toHaveBeenCalled())
    const [publicId, resourceId] = vi.mocked(listResourceBookings).mock.calls[0]
    expect(publicId).toBe(PUBLIC_ID)
    expect(resourceId).toBe(RESOURCE_ID)
  })

  it('renders the calendar grid and both panels', async () => {
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
    expect(screen.getByTestId('calendar-grid')).toBeTruthy()
    expect(screen.getByTestId('booking-panel')).toBeTruthy()
  })
})

describe("the Space's schedule", () => {
  it("greys the grid outside the Space's configured hours", async () => {
    vi.mocked(getSpaceSchedule).mockImplementation(async (_publicId, from, days) =>
      ok(uniformScheduleEntries(from, days, { slot_minutes: 30, opens_at: '09:00:00', closes_at: '17:00:00' })),
    )
    // Next week's Monday, not this one — this test cares about the
    // out-of-hours reason, not the past one, and this week's Monday may
    // already be behind `now` depending on which day the suite runs.
    const monday = addDays(startOfWeek(new Date()), DAYS_PER_WEEK)
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${toDateKey(monday)}`)

    // 08:30 (index 17) is one slot before the configured 09:00 opening;
    // 09:00 (index 18) is the first open slot. Waiting on the first covers
    // the schedule fetch resolving and the grid re-rendering with the
    // resolved schedule — the second is safe to read synchronously once it
    // has.
    await waitFor(() =>
      expect(screen.getByTestId(slotTestId(monday, 17)).dataset.blocked).toBe('out-of-hours'),
    )
    expect(screen.getByTestId(slotTestId(monday, 18)).dataset.blocked).toBeUndefined()
  })

  it("shows a notice instead of the grid when the Space's schedule can't be loaded", async () => {
    // A genuine `/schedule` fetch failure — never a per-day coherence issue,
    // which is advisory only and rendered inside `CalendarGrid` itself
    // without replacing the grid (see `CalendarGrid.test.tsx`'s own
    // "heterogeneous week" suite for that case).
    vi.mocked(getSpaceSchedule).mockResolvedValue({ outcome: 'failed', message: 'Nope.' })
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    await waitFor(() => expect(screen.getByTestId('calendar-config-notice')).toBeTruthy())
    expect(screen.queryByTestId('calendar-grid')).toBeNull()
  })
})

describe('an unparseable route', () => {
  it('reports the link as broken rather than crashing on a non-numeric resource id', () => {
    renderAt(`/s/${PUBLIC_ID}/resources/not-a-number`)

    expect(screen.getByTestId('resource-calendar-invalid')).toBeTruthy()
    expect(screen.queryByTestId('calendar-grid')).toBeNull()
  })
})

describe('the week in the URL', () => {
  // Two weeks out is safely inside the 60-day horizon regardless of when this
  // suite runs, and distinct enough from "now" that a bug reading the wrong
  // source could not accidentally land on it.
  //
  // "Today" is read through the Space's own zone (`SPACE.timezone`, 'UTC'
  // here) rather than the environment's — the same rule `week.ts`'s
  // `canGoToPreviousWeek` / `parseWeekStartParam` follow — so this stays
  // correct on a test machine whose own zone is not UTC.
  const currentWeek = () => startOfWeek(zonedCalendarDate(new Date(), SPACE.timezone))
  const twoWeeksOut = () => addDays(currentWeek(), 2 * DAYS_PER_WEEK)

  /**
   * The real instant `listResourceBookings` is called with for the week
   * starting at the calendar-date carrier `weekStartCarrier` — midnight on
   * the Space's own clock, exactly as `CalendarGrid`'s own fetch resolves it
   * via `dayBounds`. `requestedWeekStart` below reads a real instant off the
   * mock now, not a carrier, so comparisons against it go through this
   * rather than a carrier's own (environment-zone) `getTime()`.
   */
  const fetchStart = (weekStartCarrier: Date) =>
    dayBounds(weekStartCarrier, {
      slotMinutes: 30,
      openMinutes: null,
      closeMinutes: null,
      timeZone: SPACE.timezone,
    }).start

  it('renders the week named by `?week=`, not the current one', async () => {
    const target = twoWeeksOut()
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${toDateKey(target)}`)

    expect((await waitForSettledWeekStart()).getTime()).toBe(fetchStart(target).getTime())
  })

  it('normalises a real date that is not a week start to the week containing it', async () => {
    const target = addDays(twoWeeksOut(), 3)
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${toDateKey(target)}`)

    expect((await waitForSettledWeekStart()).getTime()).toBe(fetchStart(twoWeeksOut()).getTime())
  })

  it.each([
    ['not a date at all', 'banana'],
    ['an empty value', ''],
    ['a date that rolls over rather than naming a real day', '2026-02-30'],
  ])('falls back to the current week, silently, for %s', async (_label, value) => {
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${value}`)

    expect((await waitForSettledWeekStart()).getTime()).toBe(fetchStart(currentWeek()).getTime())
    // Silent: no error banner, and the grid renders as it would with no
    // `?week=` at all.
    expect(screen.queryByTestId('calendar-error')).toBeNull()
    expect(screen.getByTestId('calendar-grid')).toBeTruthy()
  })

  it('falls back to the current week for one earlier than it — navigation stays forward-only', async () => {
    const past = addDays(currentWeek(), -DAYS_PER_WEEK)
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${toDateKey(past)}`)

    expect((await waitForSettledWeekStart()).getTime()).toBe(fetchStart(currentWeek()).getTime())
  })

  it('falls back to the current week for one beyond the booking horizon', async () => {
    const beyond = addDays(startOfWeek(horizonEnd(new Date())), DAYS_PER_WEEK)
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${toDateKey(beyond)}`)

    expect((await waitForSettledWeekStart()).getTime()).toBe(fetchStart(currentWeek()).getTime())
  })

  it('writes the week into the URL when the grid pages forward', async () => {
    const { router } = renderRouterAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())
    fireEvent.click(screen.getByTestId('calendar-next-week'))

    await waitFor(() =>
      expect(new URLSearchParams(router.state.location.search).get('week')).toBe(
        toDateKey(addDays(currentWeek(), DAYS_PER_WEEK)),
      ),
    )
  })

  it('returns to the previous week on Back', async () => {
    const { router } = renderRouterAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)
    await waitFor(() => expect(screen.queryByTestId('calendar-loading')).toBeNull())

    fireEvent.click(screen.getByTestId('calendar-next-week'))
    await waitFor(() =>
      expect(requestedWeekStart(vi.mocked(listResourceBookings).mock.calls.length - 1).getTime()).toBe(
        fetchStart(addDays(currentWeek(), DAYS_PER_WEEK)).getTime(),
      ),
    )

    await act(async () => router.navigate(-1))

    await waitFor(() =>
      expect(new URLSearchParams(router.state.location.search).get('week')).toBeNull(),
    )
    await waitFor(() =>
      expect(requestedWeekStart(vi.mocked(listResourceBookings).mock.calls.length - 1).getTime()).toBe(
        fetchStart(currentWeek()).getTime(),
      ),
    )
  })
})
