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

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { getSpace, listResourceBookings, listResources } from '../api'
import type { ApiOk, Resource, Space } from '../api'
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
  created_at: '2026-07-01T00:00:00Z',
  archived_at: null,
  my_role: 'member',
}

const RESOURCE: Resource = {
  id: RESOURCE_ID,
  name: 'Court 1',
  opens_at: null,
  closes_at: null,
  slot_minutes: null,
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

beforeEach(() => {
  vi.mocked(listResourceBookings).mockResolvedValue(ok([]))
  vi.mocked(getSpace).mockResolvedValue(ok(SPACE))
  vi.mocked(listResources).mockResolvedValue(ok([RESOURCE]))
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

  it('links back to the Space', () => {
    renderAt(`/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`)

    const back = screen.getByTestId('resource-calendar-back')
    expect(back.getAttribute('href')).toBe(`/s/${PUBLIC_ID}`)
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
