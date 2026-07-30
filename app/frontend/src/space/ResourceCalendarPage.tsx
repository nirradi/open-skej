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
 * Reached only through `ResourceCalendarRoute` below, which is what `App.tsx`
 * mounts: this component itself does no auth gating and assumes a member is
 * already confirmed, since `ResourceCalendarRoute` renders it as
 * `SpaceAccessGate`'s children — the branch that only runs once the caller's
 * `previewSpace` status is `member`.
 *
 * The Space name and Resource name shown in the header are fetched for
 * display only — every request the calendar and the panels make is
 * independently authorized server-side through `require_space_role`, so a
 * stale or failed header fetch changes nothing about what is bookable. The
 * same fetch is also where `CancelPanel` gets the "may cancel anyone's
 * booking" flag it needs alongside `Booking.mine`: while it is pending or
 * failed the flag reads `false`, offering no more than a plain member could
 * already do, and it only ever widens once `space.my_role` is actually known.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { getSpace, listResources, type Booking, type Resource, type Space } from '../api'
import { BookingPanel, CancelPanel } from '../booking'
import {
  CalendarGrid,
  parseWeekStartParam,
  startOfWeek,
  toDateKey,
  type SelectedInterval,
} from '../calendar'
import { NotFoundCard, SpaceAccessGate } from './SpaceAccessGate'

/**
 * `/s/{public_id}/resources/{resource_id}`'s own door, mounted directly by
 * `App.tsx` in place of `ProtectedRoute`.
 *
 * A Resource link is forwarded exactly like a Space link — the person opening
 * it may hold no membership, no account, and have never seen `/s/{public_id}`
 * at all — so it gets the identical treatment: `SpaceAccessGate` shared with
 * `SpacePage`, admission decided at the Space (a Resource carries no
 * capability of its own — `.claude/rules/identity-and-access.md`), and the
 * calendar rendered only once the gate confirms membership.
 *
 * **Renders the preview at this Resource URL rather than redirecting to
 * `/s/{public_id}`.** That is what makes "on approval they land on the
 * Resource they were sent" true with no extra machinery: the URL a visitor
 * is sitting on already names the Resource, so once their membership exists
 * the same URL resolves straight to the calendar below. A redirect to the
 * Space page would throw that away and land them back on the picker.
 *
 * `returnTo` is this route's own full path, not `/s/{public_id}` — passed
 * explicitly for the same reason `SpaceAccessGate` requires it as a prop
 * rather than defaulting it: stating it here is what a later change to how
 * this route is mounted cannot silently break.
 */
export function ResourceCalendarRoute() {
  const { publicId, resourceId } = useParams<{ publicId: string; resourceId: string }>()

  // The route pattern makes this unreachable; TypeScript does not know that,
  // and a crash on an entry point a stranger can reach is not worth the
  // assertion.
  if (!publicId || !resourceId) {
    return <NotFoundCard />
  }

  return (
    <SpaceAccessGate publicId={publicId} returnTo={`/s/${publicId}/resources/${resourceId}`}>
      {() => <ResourceCalendarPage />}
    </SpaceAccessGate>
  )
}

type HeaderLoad = { space: Space; resource: Resource | null; resourceCount: number } | null

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

  // Read once on mount, not on every render: `now` anchors both what "the
  // current week" means and the far end of `?week=`'s valid range, and it must
  // not drift mid-session — a page that recomputed it on every render would
  // let the horizon creep forward under a caller's feet.
  const [now] = useState(() => new Date())

  const [searchParams, setSearchParams] = useSearchParams()

  /**
   * The week to render, derived from the URL rather than mirrored into a
   * `useState` — task 5.8's whole point. A `useState` seeded from `?week=`
   * would go stale the moment the URL changes by any means this component
   * did not itself cause (Back, a pasted link), which is exactly the bug this
   * shape exists to prevent. A malformed or out-of-range value reads as
   * `null` and falls back to the current week silently: it is a URL a person
   * can type.
   */
  const weekStart = useMemo(
    () => parseWeekStartParam(searchParams.get('week'), now) ?? startOfWeek(now),
    [now, searchParams],
  )

  /**
   * Pushes a new `?week=`, so Back walks week by week the way a person
   * expects. Deliberately not `replace`: that's what 5.7's single-Resource
   * redirect uses, and the two staying different is what keeps Back from
   * walking into a redirect loop on this route.
   */
  const handleWeekChange = useCallback(
    (nextWeekStart: Date) => {
      setSearchParams({ week: toDateKey(nextWeekStart) })
    },
    [setSearchParams],
  )

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
        const resourceCount = resourcesResult.outcome === 'ok' ? resourcesResult.data.length : 0
        setHeader({ space: spaceResult.data, resource, resourceCount })
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

  // `booking.mine` alone cannot tell an admin's own booking apart from a
  // member's, so `CancelPanel` also needs the caller's role — computed here,
  // the same idiom `AdminPage`'s `SpaceAdmin` uses, and `false` until the
  // header fetch above actually resolves with the Space.
  const canCancelAnyone =
    header !== null && (header.space.my_role === 'admin' || header.space.my_role === 'owner')

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
      {/*
        Hidden once the header confirms this Space has exactly one active
        Resource: 5.7 already redirects that Space straight here, so this
        link would only bounce the visitor back to a picker that immediately
        redirects them to this same page — a control that visibly does
        nothing. Shown before the header resolves and whenever there is a
        real picker (0, 2+ Resources) to return to.
      */}
      {(header === null || header.resourceCount !== 1) && (
        <Link
          to={`/s/${publicId}`}
          className="text-sm font-medium text-slate-600 hover:underline"
          data-testid="resource-calendar-back"
        >
          ← Back to Space
        </Link>
      )}

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
            now={now}
            weekStart={weekStart}
            onWeekChange={handleWeekChange}
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
            canCancelAnyone={canCancelAnyone}
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
