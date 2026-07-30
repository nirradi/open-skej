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

import { getSpace, listResourceBookings, listResources } from '../api'
import type { ApiOk, Resource, Space } from '../api'
import { addDays, DAYS_PER_WEEK, horizonEnd, startOfWeek, toDateKey } from '../calendar/week'
import { ResourceCalendarPage } from './ResourceCalendarPage'

vi.mock('../api', () => ({
  listResourceBookings: vi.fn(),
  createResourceBooking: vi.fn(),
  cancelResourceBooking: vi.fn(),
  getSpace: vi.fn(),
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

beforeEach(() => {
  vi.mocked(listResourceBookings).mockResolvedValue(ok([]))
  vi.mocked(getSpace).mockResolvedValue(ok(SPACE))
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
  const currentWeek = () => startOfWeek(new Date())
  const twoWeeksOut = () => addDays(currentWeek(), 2 * DAYS_PER_WEEK)

  it('renders the week named by `?week=`, not the current one', async () => {
    const target = twoWeeksOut()
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${toDateKey(target)}`)

    await waitFor(() => expect(listResourceBookings).toHaveBeenCalled())
    expect(requestedWeekStart(0).getTime()).toBe(target.getTime())
  })

  it('normalises a real date that is not a week start to the week containing it', async () => {
    const target = addDays(twoWeeksOut(), 3)
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${toDateKey(target)}`)

    await waitFor(() => expect(listResourceBookings).toHaveBeenCalled())
    expect(requestedWeekStart(0).getTime()).toBe(twoWeeksOut().getTime())
  })

  it.each([
    ['not a date at all', 'banana'],
    ['an empty value', ''],
    ['a date that rolls over rather than naming a real day', '2026-02-30'],
  ])('falls back to the current week, silently, for %s', async (_label, value) => {
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${value}`)

    await waitFor(() => expect(listResourceBookings).toHaveBeenCalled())
    expect(requestedWeekStart(0).getTime()).toBe(currentWeek().getTime())
    // Silent: no error banner, and the grid renders as it would with no
    // `?week=` at all.
    expect(screen.queryByTestId('calendar-error')).toBeNull()
    expect(screen.getByTestId('calendar-grid')).toBeTruthy()
  })

  it('falls back to the current week for one earlier than it — navigation stays forward-only', async () => {
    const past = addDays(currentWeek(), -DAYS_PER_WEEK)
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${toDateKey(past)}`)

    await waitFor(() => expect(listResourceBookings).toHaveBeenCalled())
    expect(requestedWeekStart(0).getTime()).toBe(currentWeek().getTime())
  })

  it('falls back to the current week for one beyond the booking horizon', async () => {
    const beyond = addDays(startOfWeek(horizonEnd(new Date())), DAYS_PER_WEEK)
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}?week=${toDateKey(beyond)}`)

    await waitFor(() => expect(listResourceBookings).toHaveBeenCalled())
    expect(requestedWeekStart(0).getTime()).toBe(currentWeek().getTime())
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
        addDays(currentWeek(), DAYS_PER_WEEK).getTime(),
      ),
    )

    await act(async () => router.navigate(-1))

    await waitFor(() =>
      expect(new URLSearchParams(router.state.location.search).get('week')).toBeNull(),
    )
    await waitFor(() =>
      expect(requestedWeekStart(vi.mocked(listResourceBookings).mock.calls.length - 1).getTime()).toBe(
        currentWeek().getTime(),
      ),
    )
  })
})
