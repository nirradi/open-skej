/**
 * `/s/{public_id}/resources/{resource_id}` — the calendar for one Resource.
 *
 * Composes `CalendarGrid` + `BookingPanel` + `CancelPanel` behind the same
 * single refresh-token pattern Stream 1's calendar page used: a booking and a
 * cancellation are the same event as far as the grid is concerned — the week
 * on screen no longer matches the server — so one counter raised by whichever
 * panel changed something is what tells the grid to refetch, rather than a
 * second mechanism for the second panel.
 *
 * Reached only through `ProtectedRoute` in `App.tsx`: only a member ever
 * holds a Resource id (they come from `listResources` on the Space page a
 * member lands on), so this component does no auth gating of its own.
 *
 * The Space name and Resource name shown in the header are fetched for
 * display only — every request the calendar and the panels make is
 * independently authorized server-side through `require_space_role`, so a
 * stale or failed header fetch changes nothing about what is bookable.
 */

import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { getSpace, listResources, type Booking, type Resource, type Space } from '../api'
import { BookingPanel, CancelPanel } from '../booking'
import { CalendarGrid, type SelectedInterval } from '../calendar'

type HeaderLoad = { space: Space; resource: Resource | null } | null

export function ResourceCalendarPage() {
  const { publicId, resourceId: resourceIdParam } = useParams<{
    publicId: string
    resourceId: string
  }>()
  const resourceId = resourceIdParam ? Number(resourceIdParam) : NaN
  const validParams = Boolean(publicId) && Number.isFinite(resourceId)

  const [selection, setSelection] = useState<SelectedInterval | null>(null)
  const [selectedBooking, setSelectedBooking] = useState<Booking | null>(null)
  const [refreshToken, setRefreshToken] = useState(0)
  const [header, setHeader] = useState<HeaderLoad>(null)

  useEffect(() => {
    if (!validParams || !publicId) return
    let cancelled = false

    void Promise.all([getSpace(publicId), listResources(publicId)]).then(
      ([spaceResult, resourcesResult]) => {
        if (cancelled || spaceResult.outcome !== 'ok') return
        const resource =
          resourcesResult.outcome === 'ok'
            ? (resourcesResult.data.find((candidate) => candidate.id === resourceId) ?? null)
            : null
        setHeader({ space: spaceResult.data, resource })
      },
    )

    return () => {
      cancelled = true
    }
  }, [publicId, resourceId, validParams])

  // Memoised deliberately: the grid notifies from an effect that depends on
  // this callback, so a fresh identity each render would re-fire it endlessly.
  const handleSelectionChange = useCallback((interval: SelectedInterval | null) => {
    setSelection(interval)
  }, [])

  const handleBookingSelect = useCallback((booking: Booking | null) => {
    setSelectedBooking(booking)
  }, [])

  const handleCalendarChanged = useCallback(() => {
    setRefreshToken((token) => token + 1)
  }, [])

  // The route pattern makes this unreachable in practice; TypeScript does not
  // know the params are well-formed, and a crash here is not worth asserting
  // around.
  if (!validParams || !publicId) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-50 p-8">
        <p className="text-sm text-red-700" role="alert" data-testid="resource-calendar-invalid">
          That calendar link doesn&rsquo;t work.
        </p>
      </main>
    )
  }

  return (
    <main className="min-h-screen bg-slate-50 p-8 text-slate-800">
      <Link
        to={`/s/${publicId}`}
        className="text-sm font-medium text-slate-600 hover:underline"
        data-testid="resource-calendar-back"
      >
        ← Back to Space
      </Link>

      <h1
        className="mt-2 text-2xl font-semibold text-slate-900"
        data-testid="resource-calendar-heading"
      >
        {header ? `${header.space.name} — ${header.resource?.name ?? 'Resource'}` : 'Calendar'}
      </h1>

      <div className="mt-6 flex flex-col gap-6 lg:flex-row lg:items-start">
        <div className="min-w-0 flex-1">
          <CalendarGrid
            publicId={publicId}
            resourceId={resourceId}
            onSelectionChange={handleSelectionChange}
            onBookingSelect={handleBookingSelect}
            refreshToken={refreshToken}
          />
        </div>
        <div className="flex flex-col gap-4 lg:w-80 lg:shrink-0">
          <CancelPanel
            publicId={publicId}
            resourceId={resourceId}
            booking={selectedBooking}
            onCalendarChanged={handleCalendarChanged}
          />
          <BookingPanel
            publicId={publicId}
            resourceId={resourceId}
            selection={selection}
            onCalendarChanged={handleCalendarChanged}
          />
        </div>
      </div>
    </main>
  )
}
