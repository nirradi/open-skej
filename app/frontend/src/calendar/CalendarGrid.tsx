/**
 * The week-view booking grid.
 *
 * ## What the grid is drawn from
 *
 * Every day column is a **minute canvas**: one relatively-positioned box
 * `1440 * pxPerMinute` tall, painted in four layers plus the bookings —
 * closed background, operating intervals, blackouts, offered-start buttons,
 * bookings — from the shape's own projection (`calendar/shape.ts`,
 * `GET /spaces/{public_id}/calendar`, `.claude/rules/calendar-shape.md`).
 * Nothing here re-derives what is bookable: the server projects, this
 * component renders exactly the table it was sent.
 *
 * **Closed is the canvas's own background; being open is the positive
 * statement.** Rendering all 24 hours as bookable and greying whatever two
 * rule types read by name said was outside hours is what drew a Space whose
 * hours came from anywhere else — a generated rule, say — as wide open, and
 * that is the reported defect this stream closes. Painting the *operating*
 * region instead means
 * the canvas can only ever offer what the projection offered. A date the
 * server did not project (`WeekProjection.forDate`'s `closedDay` fallback)
 * renders exactly like a date with an empty shape: closed, nothing offered.
 * Fail closed reaches the client too.
 *
 * ## Selection is a lookup, not arithmetic
 *
 * A day's `offered_starts` is not a uniform grid — two operating blocks with
 * different anchors (the music-room worked example: a 60-minute morning
 * block and a {60, 120}-minute evening one) can put two starts closer
 * together than either one's own shortest booking. `selection.ts` resolves a
 * click or a drag directly against that table: one offered start plus one
 * duration offered there, and nothing else is constructable. A drag that
 * cannot widen past its anchor's click unit — because a wider duration is
 * not offered, or because it would collide with a booking or the horizon —
 * simply does not widen; there is no fallback slot-index math underneath it
 * any more.
 *
 * ## What this component deliberately does not know
 *
 * **Policy rules are invisible to this grid, by design** — the deliberate
 * replacement for the "project the whole rule set onto the calendar"
 * approach `ops/plans/stream-10/OVERVIEW.md` rejects outright (decision 2).
 * A shape says what the venue *offers*; a rule says who may take it and how
 * much of it, and only the first of those is drawable without knowing who is
 * asking. A Space configuring a 120-minute `max_duration` beside a shape
 * offering 180 still lets a member build a selection the server refuses —
 * that is correct, not a bug this component owes a fix for. This grid
 * selects; it does not book, does not cancel, and does not know a single
 * policy rule exists.
 *
 * ## A window past local midnight is clamped, never drawn
 *
 * A shape's `end_time` may legitimately exceed `24:00` (a venue open past
 * midnight), and the projection represents that. This component does not
 * attempt to render the wrap: an operating or blackout interval whose
 * `end_minutes` exceeds 1440 is clipped at the day boundary, and an
 * `offered_starts` entry at or past 1440 is simply not drawn.
 * `ops/done/stream-7/passed-midnight.md` and `resolve_day_schedule`'s own
 * (now-retired) docstring record the identical limit for the pre-shape
 * calendar, and `ops/plans/stream-10/OVERVIEW.md`'s "out of scope,
 * deliberately" carries it forward — the shape can *represent* a wrapping
 * window; this grid is not asked to draw one.
 *
 * ## What this component does not do
 *
 * It selects; it does not book and it does not cancel. It reports two kinds of
 * selection upward — a free range via `onSelectionChange`, and an existing
 * booking via `onBookingSelect` — and leaves both round trips to the panels.
 *
 * `showBookings` (default `true`) is the seam a chat preview (task 10.9) reuses
 * this component through: with it off, the component issues no booking
 * request at all and draws no booking layer, so the surface a member books on
 * and the surface a draft shape is previewed with are the same component
 * rather than two implementations that could disagree.
 *
 * ## Why booking blocks can be clicked without eating the drag (task 1.8)
 *
 * Booking blocks are absolutely positioned over the offered-start buttons, so
 * until 1.8 they carried `pointer-events-none` to stop them shadowing the drag
 * handlers underneath. Simply removing that would be a real hazard — except
 * that a block can only ever cover starts that are already unselectable.
 *
 * The block's rectangle is derived from the same interval that `blockedReason`
 * reads: every offered start the interval overlaps reports `'booked'` and
 * renders `disabled`, and the block is laid out from exactly those minutes. So
 * every pixel a block occupies belongs to a start that would have refused the
 * drag anyway, and the events it now intercepts are events that previously did
 * nothing. Dragging *past* a booking is unchanged too: `dragSelection` already
 * refuses to span a blocked interval via its `isFree` predicate, so a range
 * that stopped short of a booking before still stops short of it now.
 *
 * That invariant — a block never covers a selectable start — is what makes this
 * safe, so it is asserted directly in `CalendarGrid.test.tsx` rather than left
 * to this comment. jsdom has no layout and `fireEvent` dispatches straight at
 * its target, so no test can observe CSS hit-testing; testing the invariant is
 * the thing that actually holds the guarantee up.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { listResourceBookings } from '../api'
import type { Booking } from '../api'
import { MINUTES_PER_DAY, formatMinutesLabel, pxPerMinuteFor } from '../config'
import {
  addDays,
  bookingTestId,
  canGoToNextWeek,
  canGoToPreviousWeek,
  dayBounds,
  DAYS_PER_WEEK,
  daysOfWeek,
  formatClockTime,
  intervalsOverlap,
  isSlotBeyondHorizon,
  isSlotInPast,
  localMinutesToInstant,
  slotTestId,
  startOfWeek,
  toDateKey,
  type SlotBlockedReason,
} from './week'
import { clickSelection, dragSelection, isStartInSelection, type Selection } from './selection'
import {
  buildWeekProjection,
  finestDurationMinutes,
  nextStartAfter,
  smallestDurationAt,
  type WeekProjection,
} from './shape'
import { SYSTEM_TIME_ZONE, zonedCalendarDate } from '../timezone'

const MS_PER_MINUTE = 60 * 1000

/** Shared empty list, so a non-`ok` load state does not churn memo identities. */
const NO_BOOKINGS: Booking[] = []

/** A selected range, resolved to the wall-clock interval task 1.7 will submit. */
export interface SelectedInterval {
  start: Date
  end: Date
}

/**
 * A projection that is closed on every date — the default `week` prop, and
 * the fail-closed answer for a caller that has not yet resolved a Space's
 * real shape. Built once at module scope so it does not churn identity on
 * every render of an omitted prop.
 */
const CLOSED_WEEK: WeekProjection = buildWeekProjection([], SYSTEM_TIME_ZONE)

export interface CalendarGridProps {
  /** The Space this calendar's Resource belongs to. */
  publicId: string
  /** The Resource whose bookings this grid renders. */
  resourceId: number
  /**
   * The Monday of the week to render. Owned by the caller, not this
   * component — a refresh, a bookmark, a pasted link and Back all have to
   * land on the same week, which only holds if there is exactly one place
   * that decides what "the displayed week" is, and it is not a `useState`
   * here. Previous/Next report a new value upward through `onWeekChange`
   * rather than paging an internal one.
   */
  weekStart: Date
  /**
   * The current time. Injectable so tests can sit at a fixed point relative to
   * the horizon; production passes nothing and gets a clock read once on mount.
   */
  now?: Date
  /**
   * The week's shape projection — one `DayProjection` per date plus a shared
   * `timeZone` (`calendar/shape.ts`). Defaults to `CLOSED_WEEK` when omitted:
   * every date resolves to closed, nothing offered — **fail closed**, never
   * the permissive "whole day, default slot size" a caller with no prop used
   * to get.
   */
  week?: WeekProjection
  /**
   * Whether this grid fetches and draws bookings at all. Defaults to `true`.
   *
   * With it `false`, the component issues no `listResourceBookings` request
   * and renders no booking layer — the seam the chat preview (task 10.9)
   * reuses this component through, so the surface a member books on and the
   * surface a draft shape is previewed with cannot disagree about what the
   * shape itself draws.
   */
  showBookings?: boolean
  /**
   * Notified when Previous, Next or "This week" is clicked, with the week
   * start it wants shown. This component does not act on its own click —
   * the caller decides whether and how the visible week actually changes
   * (in practice, by writing `?week=` and re-rendering with a new prop).
   */
  onWeekChange?: (weekStart: Date) => void
  /** Notified whenever the selected range changes. Task 1.7's entry point. */
  onSelectionChange?: (interval: SelectedInterval | null) => void
  /**
   * Notified whenever the selected *existing booking* changes. Task 1.8's entry
   * point, and the mirror image of `onSelectionChange`: one reports free time
   * the user wants, the other reports booked time the user may want back.
   *
   * The two are mutually exclusive — selecting either clears the other — because
   * a single panel column cannot sensibly offer "book this" and "cancel that" at
   * the same time.
   */
  onBookingSelect?: (booking: Booking | null) => void
  /**
   * Bump to refetch the displayed week and drop the current selection.
   *
   * Task 1.7 raises this after a booking is created, and after an `overlap`
   * denial — which means the week on screen is stale and the conflicting
   * booking is not yet drawn. Task 1.8 raises it after a cancellation, where the
   * refetch is what frees the slot for rebooking without a page reload.
   * Refetching rather than splicing the change in locally keeps the grid showing
   * what the server actually holds; the cost is one request, and the benefit is
   * that the optimistic and authoritative views cannot drift apart.
   *
   * Both selections are dropped with it. For a range, the minutes it covers have
   * just become unbookable — either we booked them or somebody else did. For a
   * booking, it has just been cancelled and is about to stop existing, so
   * leaving it selected would offer a second cancel of a booking that is gone.
   */
  refreshToken?: number
}

/**
 * What the grid knows about the bookings for the displayed week.
 *
 * `key` identifies the request the state answers. A settled state whose key no
 * longer matches the week on screen is stale and reads as `loading` again —
 * derived during render rather than reset by an effect, which keeps navigation
 * from briefly showing last week's bookings against this week's grid.
 */
type LoadState =
  | { status: 'ok'; key: string; bookings: Booking[] }
  | { status: 'error'; key: string; message: string }

/** Copy for a fetch that failed in a way the user cannot act on. */
const LOAD_ERROR_FALLBACK = "We couldn't load this week's bookings."

/**
 * The height of the day-header row, shared by the header itself and the time
 * axis's own spacer above the hour labels.
 *
 * One constant rather than the same class written twice: the axis and the day
 * columns sit side by side in normal flow, so the moment those two heights
 * disagree every hour label is offset from the minutes it names — a
 * misalignment jsdom cannot observe (it performs no layout) and no test here
 * can catch.
 */
const HEADER_ROW_HEIGHT_CLASS = 'h-8'

/** Where a drag started: one offered start, pinned to the day it began on. */
interface Anchor {
  dateKey: string
  startMinutes: number
}

// `dayHeaderFormat` / `weekLabelFormat` format calendar-date carriers (see
// `week.ts`'s docblock), not real instants tied to a Space — formatting one
// with the environment's own default zone reads back exactly the
// `(year, month, day)` triple it was built from, regardless of which Space
// is on screen, so neither needs an explicit `timeZone`.
const dayHeaderFormat = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric' })
const weekLabelFormat = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
})

/** A real instant as `Mon DD, YYYY, HH:MM`, resolved in an explicit `timeZone`. */
function formatZonedDateTime(value: Date, timeZone: string): string {
  const day = new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone,
  }).format(value)
  return `${day}, ${formatClockTime(value, timeZone)}`
}

export function CalendarGrid({
  publicId,
  resourceId,
  weekStart,
  now: nowProp,
  week,
  showBookings = true,
  onWeekChange,
  onSelectionChange,
  onBookingSelect,
  refreshToken = 0,
}: CalendarGridProps) {
  const resolvedWeek = week ?? CLOSED_WEEK
  const timeZone = resolvedWeek.timeZone

  const [fallbackNow] = useState(() => new Date())
  const now = nowProp ?? fallbackNow

  const [reloadNonce, setReloadNonce] = useState(0)
  const [settled, setSettled] = useState<LoadState | null>(null)
  const [anchor, setAnchor] = useState<Anchor | null>(null)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [selectedBookingId, setSelectedBookingId] = useState<number | null>(null)

  // Drop both selections when the parent asks for a refresh. Adjusted during
  // render rather than in an effect: an effect would let one frame paint with a
  // selection highlighting minutes that are about to come back as booked.
  const [seenRefreshToken, setSeenRefreshToken] = useState(refreshToken)
  if (seenRefreshToken !== refreshToken) {
    setSeenRefreshToken(refreshToken)
    setSelection(null)
    setAnchor(null)
    setSelectedBookingId(null)
  }

  // Drop both selections when the displayed week changes, by whatever means —
  // the buttons below, but just as much Back, a pasted link, or a parent that
  // re-resolves `?week=` for any other reason. A selection is a start plus a
  // duration plus a date key, and carrying it across to a week that may not
  // even share that date would leave it pointing at nothing on screen. Keyed
  // off the prop itself, in render, rather than an effect on it or a clear
  // inside the button handlers below: this component cannot tell "the
  // buttons changed it" apart from "the caller changed it", and after task
  // 5.8 it must not need to.
  const weekKey = weekStart.getTime()
  const [seenWeekKey, setSeenWeekKey] = useState(weekKey)
  if (seenWeekKey !== weekKey) {
    setSeenWeekKey(weekKey)
    setSelection(null)
    setAnchor(null)
    setSelectedBookingId(null)
  }

  /**
   * Identifies the fetch the grid currently wants an answer to.
   *
   * Includes `timeZone`: the fetch window below is resolved through it, so a
   * zone swap (the placeholder zone giving way to the Space's real one,
   * before `weekStart` or either token has changed) is a genuinely different
   * request, not the same one settling twice.
   */
  const requestKey = `${weekStart.getTime()}:${reloadNonce}:${refreshToken}:${timeZone}`
  const load: LoadState | { status: 'loading' } = !showBookings
    ? { status: 'ok', key: requestKey, bookings: NO_BOOKINGS }
    : settled !== null && settled.key === requestKey
      ? settled
      : { status: 'loading' }

  const days = useMemo(() => daysOfWeek(weekStart), [weekStart])
  const dateKeys = useMemo(() => days.map(toDateKey), [days])
  const dayProjections = useMemo(
    () => dateKeys.map((key) => resolvedWeek.forDate(key)),
    [resolvedWeek, dateKeys],
  )

  /**
   * The shared minute-canvas pixel scale — the finest duration offered
   * anywhere in the visible week (`calendar/shape.ts`'s `finestDurationMinutes`;
   * see this file's module docblock for why the grid is no longer a uniform
   * slot count). At the pre-shape default (30-minute steps), this renders at
   * exactly the height the old slot grid did.
   */
  const finestMinutes = useMemo(() => finestDurationMinutes(resolvedWeek, dateKeys), [resolvedWeek, dateKeys])
  const pxPerMinute = pxPerMinuteFor(finestMinutes)
  const dayHeight = MINUTES_PER_DAY * pxPerMinute

  // ---- Loading the week's bookings -------------------------------------

  useEffect(() => {
    if (!showBookings) return
    let cancelled = false
    // Midnight-to-midnight on the Space's own clock, not the environment's —
    // the same reason `bookingsByDay` below goes through `dayBounds` rather
    // than `weekStart` / `addDays` directly.
    const from = dayBounds(weekStart, timeZone).start
    const to = dayBounds(addDays(weekStart, DAYS_PER_WEEK - 1), timeZone).end

    void listResourceBookings(publicId, resourceId, from, to).then((result) => {
      // A response for a week the user has already navigated away from would
      // otherwise overwrite the newer one if it happened to land second.
      if (cancelled) return
      switch (result.outcome) {
        case 'ok':
          setSettled({ status: 'ok', key: requestKey, bookings: result.data })
          break
        case 'failed':
        case 'unauthenticated':
        case 'forbidden':
        case 'not_found':
          // Every one of these leaves the grid without a trustworthy answer
          // about what is already booked, so all four are the same "error"
          // state to the grid — fail-closed, exactly as a network failure is.
          setSettled({ status: 'error', key: requestKey, message: result.message })
          break
        case 'invalid_request':
          // A client bug, not something the user did — `detail` is diagnostic
          // text, so it is logged rather than shown as friendly copy.
          console.error(
            'listResourceBookings rejected the calendar window',
            result.detail,
            result.raw,
          )
          setSettled({ status: 'error', key: requestKey, message: LOAD_ERROR_FALLBACK })
          break
      }
    })

    return () => {
      cancelled = true
    }
    // `weekStart` and `timeZone` are read inside this effect only to compute
    // `from` / `to`, and `requestKey` already encodes both by value — see its
    // own docblock. Listing them here instead would re-fire this fetch on a
    // fresh-but-equal `Date`, exactly the hazard `ResourceCalendarPage`'s own
    // `weekStart` stabiliser exists to prevent one layer up.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [publicId, requestKey, resourceId, showBookings])

  /**
   * The bookings shown, per day.
   *
   * Empty while loading and on error — but note the grid does *not* then render
   * as a week of free starts: `blockedReason` treats both states as unselectable,
   * so a failed fetch cannot be mistaken for an empty calendar and clicked into
   * a double booking.
   */
  const bookings = load.status === 'ok' ? load.bookings : NO_BOOKINGS

  const bookingsByDay = useMemo(() => {
    const parsed = bookings.map((booking) => ({
      booking,
      start: new Date(booking.start_at),
      end: new Date(booking.end_at),
    }))

    return days.map((day) => {
      // Midnight to midnight on the Space's own clock — a booking that
      // straddles the environment's midnight but not the Space's must still
      // group with the one day column it actually belongs to.
      const { start: dayStart, end: dayEnd } = dayBounds(day, timeZone)
      return parsed.filter((entry) => intervalsOverlap(entry.start, entry.end, dayStart, dayEnd))
    })
  }, [bookings, days, timeZone])

  // ---- What a user may click -------------------------------------------

  /**
   * Whether the offered start at `startMinutes` on day `dayIndex` may be
   * selected, and if not, why — asked about the start plus its own click
   * unit (the smallest duration offered there), never a slot index.
   */
  const blockedReason = useCallback(
    (dayIndex: number, startMinutes: number): SlotBlockedReason | null => {
      // Until the week's bookings are known, every start is unselectable. The
      // alternative — an optimistically empty grid — invites a booking against
      // data we do not have.
      if (load.status !== 'ok') return 'unavailable'

      const day = days[dayIndex]
      const dayProjection = dayProjections[dayIndex]
      const duration = smallestDurationAt(dayProjection, startMinutes)
      if (duration === null) return 'unavailable'

      const start = localMinutesToInstant(day, startMinutes, timeZone)
      const end = localMinutesToInstant(day, startMinutes + duration, timeZone)
      if (isSlotInPast(start, now)) return 'past'
      if (isSlotBeyondHorizon(start, now)) return 'beyond-horizon'

      const covering = bookingsByDay[dayIndex].some((entry) =>
        intervalsOverlap(start, end, entry.start, entry.end),
      )
      return covering ? 'booked' : null
    },
    [bookingsByDay, dayProjections, days, load.status, now, timeZone],
  )

  // ---- Selection --------------------------------------------------------

  const selectedInterval = useMemo((): SelectedInterval | null => {
    if (selection === null) return null
    const dayIndex = days.findIndex((day) => toDateKey(day) === selection.dateKey)
    if (dayIndex === -1) return null
    const day = days[dayIndex]
    const start = localMinutesToInstant(day, selection.startMinutes, timeZone)
    const end = localMinutesToInstant(day, selection.startMinutes + selection.durationMinutes, timeZone)
    return { start, end }
  }, [days, selection, timeZone])

  useEffect(() => {
    onSelectionChange?.(selectedInterval)
  }, [onSelectionChange, selectedInterval])

  // ---- Selecting an existing booking (to cancel it) ---------------------

  /**
   * The selected booking, resolved against the week currently loaded.
   *
   * Derived from an id rather than held as an object so it cannot outlive the
   * booking it names: once a cancellation lands and the refetch returns a list
   * without it, this resolves to `null` on its own and the cancel panel stops
   * offering to cancel something that is already gone.
   */
  const selectedBooking = useMemo(
    () => bookings.find((candidate) => candidate.id === selectedBookingId) ?? null,
    [bookings, selectedBookingId],
  )

  useEffect(() => {
    onBookingSelect?.(selectedBooking)
  }, [onBookingSelect, selectedBooking])

  /** Clicking a block selects it; clicking it again puts the panel away. */
  const toggleBooking = useCallback((bookingId: number) => {
    setSelectedBookingId((current) => (current === bookingId ? null : bookingId))
    // A booking and a free range are two answers to the same question, so
    // picking one retracts the other.
    setSelection(null)
    setAnchor(null)
  }, [])

  const extendTo = useCallback(
    (dayIndex: number, startMinutes: number) => {
      if (anchor === null) return
      const day = days[dayIndex]
      // Selection is confined to the day it started on: a booking is one
      // interval, and starts on another column are a different day.
      if (toDateKey(day) !== anchor.dateKey) return

      const dayProjection = dayProjections[dayIndex]
      const isFree = (s: number, e: number): boolean => {
        const start = localMinutesToInstant(day, s, timeZone)
        const end = localMinutesToInstant(day, e, timeZone)
        if (isSlotInPast(start, now)) return false
        if (isSlotBeyondHorizon(start, now)) return false
        return !bookingsByDay[dayIndex].some((entry) =>
          intervalsOverlap(start, end, entry.start, entry.end),
        )
      }

      const next = dragSelection(dayProjection, anchor.startMinutes, startMinutes, isFree)
      if (next === null) return
      setSelection(next)
    },
    [anchor, bookingsByDay, dayProjections, days, now, timeZone],
  )

  const beginAt = useCallback(
    (dayIndex: number, startMinutes: number) => {
      if (blockedReason(dayIndex, startMinutes) !== null) return
      const dateKey = toDateKey(days[dayIndex])
      const next = clickSelection(dayProjections[dayIndex], startMinutes)
      if (next === null) return
      setAnchor({ dateKey, startMinutes })
      setSelection(next)
      setSelectedBookingId(null)
    },
    [blockedReason, dayProjections, days],
  )

  // The drag ends wherever the pointer is released, including outside the grid
  // or outside the window — without this, releasing over the page chrome would
  // leave the grid stuck in a dragging state and selecting on plain hover.
  useEffect(() => {
    if (anchor === null) return
    const end = () => setAnchor(null)
    window.addEventListener('pointerup', end)
    window.addEventListener('pointercancel', end)
    return () => {
      window.removeEventListener('pointerup', end)
      window.removeEventListener('pointercancel', end)
    }
  }, [anchor])

  // ---- Navigation -------------------------------------------------------

  const canPrev = canGoToPreviousWeek(weekStart, now, timeZone)
  const canNext = canGoToNextWeek(weekStart, now, timeZone)
  const thisWeek = startOfWeek(zonedCalendarDate(now, timeZone))
  const isCurrentWeek = weekStart.getTime() === thisWeek.getTime()

  // Shown only when it differs from the Space's own zone (the module
  // docblock's "secondary hint, never a second version of the grid") — the
  // one place the viewer's own zone appears in this component at all.
  const viewerTimeZone = SYSTEM_TIME_ZONE
  const showViewerTimeZoneHint = viewerTimeZone !== timeZone

  /** Reports the week `deltaWeeks` away from the one currently shown. */
  const goToWeek = (deltaWeeks: number) => {
    onWeekChange?.(addDays(weekStart, deltaWeeks * DAYS_PER_WEEK))
  }

  /** Reports the current week — a no-op while it is already the one shown. */
  const goToThisWeek = () => {
    if (!isCurrentWeek) onWeekChange?.(thisWeek)
  }

  return (
    <section className="flex flex-col gap-3" data-testid="calendar">
      <header className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2">
          <button
            type="button"
            data-testid="calendar-prev-week"
            className="rounded border border-slate-300 px-3 py-1 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
            disabled={!canPrev}
            onClick={() => goToWeek(-1)}
          >
            ← Previous
          </button>
          <button
            type="button"
            data-testid="calendar-next-week"
            className="rounded border border-slate-300 px-3 py-1 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
            disabled={!canNext}
            onClick={() => goToWeek(1)}
          >
            Next →
          </button>
          <button
            type="button"
            data-testid="calendar-this-week"
            className="rounded border border-slate-300 px-3 py-1 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
            disabled={isCurrentWeek}
            onClick={goToThisWeek}
          >
            This week
          </button>
        </div>
        <h2 className="text-sm font-medium text-slate-700" data-testid="calendar-week-label">
          {weekLabelFormat.format(weekStart)} – {weekLabelFormat.format(addDays(weekStart, 6))}
        </h2>
      </header>

      <p className="text-xs text-slate-500" data-testid="calendar-timezone-note">
        Times shown in {timeZone}
        {showViewerTimeZoneHint && ` — your own zone is ${viewerTimeZone}`}
      </p>

      {load.status === 'loading' && (
        <p className="text-sm text-slate-500" data-testid="calendar-loading">
          Loading this week's bookings…
        </p>
      )}

      {load.status === 'error' && (
        <div
          role="alert"
          data-testid="calendar-error"
          className="flex items-center justify-between gap-4 rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800"
        >
          <span>
            {load.message} Slots are disabled until we know what is already booked, so nothing gets
            double-booked.
          </span>
          <button
            type="button"
            data-testid="calendar-retry"
            className="rounded border border-red-400 px-2 py-1 font-medium hover:bg-red-100"
            onClick={() => setReloadNonce((n) => n + 1)}
          >
            Retry
          </button>
        </div>
      )}

      <div
        data-testid="calendar-grid"
        className="flex select-none overflow-x-auto rounded border border-slate-200 bg-white"
      >
        {/* Time axis. One label per hour, sized in minutes on the shared
            pixel scale — a heterogeneous day cannot be labelled per-slot the
            way a uniform grid could. */}
        <div className="sticky left-0 z-10 shrink-0 border-r border-slate-200 bg-white">
          <div className={`${HEADER_ROW_HEIGHT_CLASS} border-b border-slate-200`} />
          {Array.from({ length: 24 }, (_, hour) => (
            <div
              key={hour}
              style={{ height: 60 * pxPerMinute }}
              className="flex items-start justify-end px-2 text-[10px] leading-none text-slate-400 tabular-nums"
            >
              {formatMinutesLabel(hour * 60)}
            </div>
          ))}
        </div>

        {days.map((day, dayIndex) => {
          const dateKey = toDateKey(day)
          const dayProjection = dayProjections[dayIndex]
          // Not drawn past local midnight — see the module docblock's
          // "clamped, never wrapped" section.
          const offeredStarts = dayProjection.offeredStarts.filter(
            (offered) => offered.startMinutes < MINUTES_PER_DAY,
          )
          // Midnight on the Space's own clock — see `bookingsByDay` above for
          // why this must not be the environment's midnight.
          const { start: dayStart } = dayBounds(day, timeZone)

          return (
            <div key={dateKey} className="min-w-24 flex-1 border-r border-slate-200 last:border-r-0">
              <div className="border-b border-slate-200">
                <div
                  className={`flex ${HEADER_ROW_HEIGHT_CLASS} items-center justify-center text-xs font-medium text-slate-600`}
                  data-testid={`calendar-day-${dateKey}`}
                >
                  {dayHeaderFormat.format(day)}
                </div>
              </div>

              <div
                className="relative bg-slate-100"
                style={{ height: dayHeight }}
                data-testid={`calendar-day-column-${dateKey}`}
                data-offered-starts={offeredStarts.length}
              >
                {/* Operating intervals — painted over the closed background.
                    Clipped to [0, 1440]: an interval whose end exceeds it
                    represents a window past local midnight (the shape
                    schema permits this) which this grid does not draw. */}
                {dayProjection.operatingIntervals.map((interval, index) => {
                  const start = Math.max(0, interval.startMinutes)
                  const end = Math.min(MINUTES_PER_DAY, interval.endMinutes)
                  if (end <= start) return null
                  return (
                    <div
                      key={index}
                      className="absolute inset-x-0 bg-white"
                      style={{ top: start * pxPerMinute, height: (end - start) * pxPerMinute }}
                    />
                  )
                })}

                {/* Blackouts — greyed over the operating paint, with their
                    own reason rendered visibly (and as `title`), clipped the
                    same way. */}
                {dayProjection.blackoutIntervals.map((interval, index) => {
                  const start = Math.max(0, interval.startMinutes)
                  const end = Math.min(MINUTES_PER_DAY, interval.endMinutes)
                  if (end <= start) return null
                  return (
                    <div
                      key={index}
                      data-testid={`blackout-${dateKey}-${interval.startMinutes}`}
                      title={interval.reason}
                      className="absolute inset-x-0 flex items-center justify-center overflow-hidden bg-slate-400/70 px-1 text-center text-[9px] leading-tight text-slate-800"
                      style={{ top: start * pxPerMinute, height: (end - start) * pxPerMinute }}
                    >
                      {interval.reason}
                    </div>
                  )
                })}

                {/* Offered starts — one button per `offered_starts` entry.
                    Height is that start's own click unit (the smallest
                    duration offered there), capped so it never runs past the
                    next offered start — two blocks with different anchors
                    can put two starts closer together than either one's
                    shortest booking, and every offered start must stay
                    clickable. */}
                {offeredStarts.map((offered) => {
                  const startMinutes = offered.startMinutes
                  const clickDuration = smallestDurationAt(dayProjection, startMinutes) ?? 0
                  const nextStart = nextStartAfter(dayProjection, startMinutes)
                  const cappedDuration =
                    nextStart !== null ? Math.min(clickDuration, nextStart - startMinutes) : clickDuration
                  const top = startMinutes * pxPerMinute
                  const height = Math.max(
                    0,
                    Math.min(cappedDuration, MINUTES_PER_DAY - startMinutes) * pxPerMinute,
                  )
                  const blocked = blockedReason(dayIndex, startMinutes)
                  const selected = isStartInSelection(selection, dateKey, startMinutes)

                  return (
                    <button
                      key={startMinutes}
                      type="button"
                      data-testid={slotTestId(day, startMinutes)}
                      data-blocked={blocked ?? undefined}
                      data-selected={selected || undefined}
                      aria-pressed={selected}
                      aria-label={`${dateKey} ${formatMinutesLabel(startMinutes)}`}
                      disabled={blocked !== null}
                      style={{ top, height }}
                      // A blocked start is greyed rather than left
                      // transparent: it sits *inside* an operating region, so
                      // the white paint underneath would otherwise read as
                      // "open" for time that has already passed or is
                      // already booked. Grey is the same colour closed time
                      // carries, which is the honest reading — this minute is
                      // not available, whatever the venue's hours say.
                      className={[
                        'absolute inset-x-0 block border-b border-slate-100 text-left',
                        selected
                          ? 'bg-sky-500'
                          : blocked === null
                            ? 'hover:bg-sky-100'
                            : 'cursor-not-allowed bg-slate-100',
                      ].join(' ')}
                      onPointerDown={() => beginAt(dayIndex, startMinutes)}
                      onPointerOver={() => extendTo(dayIndex, startMinutes)}
                    />
                  )
                })}

                {showBookings &&
                  bookingsByDay[dayIndex].map(({ booking, start, end }) => {
                    // Minutes from midnight — the grid always renders the full
                    // day starting there, whatever the Space's own hours are.
                    const startMinutes = (start.getTime() - dayStart.getTime()) / MS_PER_MINUTE
                    const endMinutes = (end.getTime() - dayStart.getTime()) / MS_PER_MINUTE
                    const top = Math.max(0, startMinutes * pxPerMinute)
                    const bottom = Math.min(dayHeight, endMinutes * pxPerMinute)
                    // A booking rendered on a day it did not start on is a
                    // continuation, and must not duplicate the canonical testid.
                    const isContinuation = start.getTime() < dayStart.getTime()
                    const isSelected = booking.id === selectedBookingId

                    return (
                      <button
                        key={booking.id}
                        type="button"
                        data-testid={
                          isContinuation
                            ? `${bookingTestId(booking.id)}-continued`
                            : bookingTestId(booking.id)
                        }
                        data-booking-id={booking.id}
                        data-selected={isSelected || undefined}
                        data-mine={booking.mine || undefined}
                        aria-pressed={isSelected}
                        aria-label={
                          booking.mine
                            ? `Booked ${formatClockTime(start, timeZone)} to ${formatClockTime(end, timeZone)}`
                            : `Booked by someone else, ${formatClockTime(start, timeZone)} to ${formatClockTime(end, timeZone)}`
                        }
                        // Interactive as of 1.8, and safe to be: see the note at
                        // the top of this file on why intercepting these pointer
                        // events cannot cost the grid a drag. Not-mine bookings
                        // stay clickable too — selection is how an admin sees
                        // when it runs before acting on it, and the ownership
                        // check that actually gates cancelling lives server-side
                        // and in `CancelPanel`, not here.
                        className={[
                          'absolute inset-x-0.5 overflow-hidden rounded px-1 text-left text-[10px] leading-tight text-white',
                          booking.mine
                            ? isSelected
                              ? 'bg-indigo-700 ring-2 ring-indigo-900'
                              : 'bg-indigo-500 hover:bg-indigo-600'
                            : isSelected
                              ? 'bg-slate-600 ring-2 ring-slate-800'
                              : 'bg-slate-400 hover:bg-slate-500',
                        ].join(' ')}
                        style={{ top, height: Math.max(0, bottom - top) }}
                        onClick={() => toggleBooking(booking.id)}
                      >
                        {formatClockTime(start, timeZone)}
                      </button>
                    )
                  })}
              </div>
            </div>
          )
        })}
      </div>

      {selection !== null && selectedInterval !== null && (
        <p className="text-sm text-slate-700" data-testid="calendar-selection">
          Selected {selection.durationMinutes} minute{selection.durationMinutes === 1 ? '' : 's'}:{' '}
          {formatZonedDateTime(selectedInterval.start, timeZone)} –{' '}
          {formatClockTime(selectedInterval.end, timeZone)}
        </p>
      )}
    </section>
  )
}
