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
import { buildCalendarConfig } from '../config'
import { SYSTEM_TIME_ZONE, zonedCalendarDate } from '../timezone'
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
   * The zone every displayed and submitted time on this page resolves
   * through: the Space's own `timezone` once the header fetch resolves it,
   * and the environment's own zone as a bootstrapping placeholder before
   * that — the same placeholder `calendarConfig`'s own module default uses,
   * for the same reason: there is no Space yet to have a real zone. Never a
   * viewer preference — see `timezone.ts` and `ops/DEFERRED.md` item 19 for
   * why that was rejected outright.
   */
  const timeZone = header?.space.timezone ?? SYSTEM_TIME_ZONE

  /**
   * The week to render, derived from the URL rather than mirrored into a
   * `useState` — task 5.8's whole point. A `useState` seeded from `?week=`
   * would go stale the moment the URL changes by any means this component
   * did not itself cause (Back, a pasted link), which is exactly the bug this
   * shape exists to prevent. A malformed or out-of-range value reads as
   * `null` and falls back to the current week silently: it is a URL a person
   * can type.
   *
   * "The current week" means the Space's today, read through `timeZone` —
   * see `week.ts`'s `zonedCalendarDate` — so the fallback here and the grid's
   * own navigation bounds can never name two different current weeks.
   *
   * Stabilised by *value*, not merely memoised, because `timeZone` itself
   * changes exactly once in a normal session — from the bootstrapping
   * placeholder to the Space's real zone, the moment the header fetch
   * resolves (see `timeZone` above). `useMemo` alone would hand `CalendarGrid`
   * a freshly-constructed `Date` at that moment even when the placeholder and
   * the real zone name the identical calendar week (the common case — most
   * viewers are not near a day boundary), and `CalendarGrid`'s own booking
   * fetch keys an effect off this object's identity, not just its value: a
   * new-but-equal `Date` re-fires that effect and hands a booking's second,
   * empty mock response to a test — or a real fetch — that had no reason to
   * expect one. Comparing by `getTime()` here and only replacing the stored
   * `Date` when the value actually differs is what keeps `weekStart`
   * referentially stable across that transition whenever the two zones agree
   * on what "this week" is.
   */
  const computedWeekStart = useMemo(
    () =>
      parseWeekStartParam(searchParams.get('week'), now, timeZone) ??
      startOfWeek(zonedCalendarDate(now, timeZone)),
    [now, searchParams, timeZone],
  )
  const [weekStart, setWeekStart] = useState(computedWeekStart)
  if (weekStart.getTime() !== computedWeekStart.getTime()) {
    setWeekStart(computedWeekStart)
  }

  /**
   * Pushes a new `?week=`, so Back walks week by week the way a person
   * expects. Deliberately not `replace`: that's what 5.7's single-Resource
   * redirect uses, and the two staying different is what keeps Back from
   * walking into a redirect loop on this route.
   */
  const handleWeekChange = useCallback(
    (nextWeekStart: Date) => {
      // Sets `week` on the existing params rather than replacing the whole
      // object with `{ week }`. There is no other query parameter on this
      // route today, so the two behave identically — and the day one is added
      // the object form drops it silently, which is a bug that presents as
      // "paging the calendar forgets something unrelated" and reads as
      // anything but a line in this callback.
      setSearchParams((current) => {
        const next = new URLSearchParams(current)
        next.set('week', toDateKey(nextWeekStart))
        return next
      })
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

  /**
   * The grid's layout, built from this Space's own schedule once the header
   * fetch resolves it. `null` while pending, so `CalendarGrid` renders on its
   * own fallback (the whole day, unrestricted) rather than waiting on a
   * second request it does not otherwise need — the same "independent of the
   * header fetch" property the component's own docblock already claims.
   *
   * An `'incoherent'` result (see `coherenceIssue` in `config.ts`) means this
   * Space's `slot_minutes` / `opens_at` / `closes_at` cannot describe a valid
   * grid — a bad admin edit, or the `DEFERRED.md` item 18 shape where a
   * Space's local hours cross its own local midnight and are unrepresentable
   * in the first place. Rather than guess at a grid from data that cannot
   * produce one, the calendar is replaced with a notice: the grid must never
   * offer what the server will refuse, and a best-effort window built from
   * nonsense data is exactly that risk.
   *
   * Memoized on `header`: `CalendarGrid` puts this in the dependency array of
   * both `selectedInterval` and the effect that reports it upward through
   * `onSelectionChange`, so a fresh object here on every render — even one
   * describing the same schedule — retriggers that effect, which calls back
   * into this component's own `setSelection` and re-renders it, rebuilding
   * this object again. That feedback loop doesn't converge: selecting a
   * single slot hangs the page in an infinite render loop.
   */
  const configResult = useMemo(
    () => (header !== null ? buildCalendarConfig(header.space) : null),
    [header],
  )

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
          {configResult?.status === 'incoherent' ? (
            <p
              role="alert"
              data-testid="calendar-config-notice"
              className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800"
            >
              This Space&rsquo;s schedule can&rsquo;t be shown: {configResult.message} An admin
              needs to fix it in Schedule settings before this calendar can be used.
            </p>
          ) : (
            <CalendarGrid
              publicId={publicId}
              resourceId={resourceId}
              now={now}
              weekStart={weekStart}
              config={configResult?.status === 'ok' ? configResult.config : undefined}
              onWeekChange={handleWeekChange}
              onSelectionChange={handleSelectionChange}
              onBookingSelect={handleBookingSelect}
              refreshToken={refreshToken}
            />
          )}
        </div>
        <div className="flex flex-col gap-4 lg:w-80 lg:shrink-0">
          <CancelPanel
            publicId={publicId}
            resourceId={resourceId}
            booking={selectedBooking}
            canCancelAnyone={canCancelAnyone}
            onCalendarChanged={handleCalendarChanged}
            timeZone={timeZone}
          />
          <BookingPanel
            publicId={publicId}
            resourceId={resourceId}
            selection={selection}
            onCalendarChanged={handleCalendarChanged}
            timeZone={timeZone}
          />
        </div>
      </div>
    </main>
  )
}
