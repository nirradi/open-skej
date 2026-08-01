/**
 * The week-view booking grid.
 *
 * ## What drives the layout
 *
 * Everything: slot count, slot labels, row height and the vertical position of
 * a booking block all derive from `config.ts`. There is exactly one hardcoded
 * dimension here — `SLOT_ROW_HEIGHT_PX`, the height of *one slot*, whatever a
 * slot happens to be. Changing `slotMinutes` from 30 to 10 triples the rows and
 * re-lays out the bookings with no edit to this file, which is what the test
 * suite asserts rather than leaving to inspection.
 *
 * ## What this component does not do
 *
 * It selects; it does not book and it does not cancel. It reports two kinds of
 * selection upward — a free range via `onSelectionChange`, and an existing
 * booking via `onBookingSelect` — and leaves both round trips to the panels.
 *
 * ## Why booking blocks can be clicked without eating the drag (task 1.8)
 *
 * Booking blocks are absolutely positioned over the slot buttons, so until 1.8
 * they carried `pointer-events-none` to stop them shadowing the drag handlers
 * underneath. Simply removing that would be a real hazard — except that a block
 * can only ever cover slots that are already unselectable.
 *
 * The block's rectangle is derived from the same interval that `blockedReason`
 * reads: every slot the interval overlaps reports `'booked'` and renders
 * `disabled`, and the block is laid out from exactly those minutes. So every
 * pixel a block occupies belongs to a slot that would have refused the drag
 * anyway, and the events it now intercepts are events that previously did
 * nothing. Dragging *past* a booking is unchanged too: `rangeBetween` already
 * refused to span a blocked slot, so a range that stopped short of a booking
 * before still stops short of it now.
 *
 * That invariant — a block never covers a selectable slot — is what makes this
 * safe, so it is asserted directly in `CalendarGrid.test.tsx` rather than left
 * to this comment. jsdom has no layout and `fireEvent` dispatches straight at
 * its target, so no test can observe CSS hit-testing; testing the invariant is
 * the thing that actually holds the guarantee up.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { listResourceBookings } from '../api'
import type { Booking } from '../api'
import {
  calendarConfig,
  formatSlotLabel,
  isSlotOutOfHours,
  slotsPerDayFor,
  type CalendarConfig,
} from '../config'
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
  slotInterval,
  slotTestId,
  startOfWeek,
  toDateKey,
  type SlotBlockedReason,
} from './week'
import { isInSelection, rangeBetween, rangeLength, type Selection } from './selection'
import { SYSTEM_TIME_ZONE, zonedCalendarDate } from '../timezone'

/**
 * Height of a single slot row, in pixels.
 *
 * Per *slot*, not per half-hour: at a 10-minute granularity the day is three
 * times as tall, which is the honest consequence of asking for three times the
 * resolution.
 */
const SLOT_ROW_HEIGHT_PX = 28

const MS_PER_MINUTE = 60 * 1000

/** Shared empty list, so a non-`ok` load state does not churn memo identities. */
const NO_BOOKINGS: Booking[] = []

/** A selected range, resolved to the wall-clock interval task 1.7 will submit. */
export interface SelectedInterval {
  start: Date
  end: Date
}

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
  /** Calendar configuration. Defaults to the module singleton in `config.ts`. */
  config?: CalendarConfig
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
   * Both selections are dropped with it. For a range, the slots it covers have
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
  config,
  onWeekChange,
  onSelectionChange,
  onBookingSelect,
  refreshToken = 0,
}: CalendarGridProps) {
  const resolvedConfig = config ?? calendarConfig
  const [fallbackNow] = useState(() => new Date())
  const now = nowProp ?? fallbackNow

  const [reloadNonce, setReloadNonce] = useState(0)
  const [settled, setSettled] = useState<LoadState | null>(null)
  const [anchor, setAnchor] = useState<{ dateKey: string; index: number } | null>(null)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [selectedBookingId, setSelectedBookingId] = useState<number | null>(null)

  // Drop both selections when the parent asks for a refresh. Adjusted during
  // render rather than in an effect: an effect would let one frame paint with a
  // selection highlighting slots that are about to come back as booked.
  const [seenRefreshToken, setSeenRefreshToken] = useState(refreshToken)
  if (seenRefreshToken !== refreshToken) {
    setSeenRefreshToken(refreshToken)
    setSelection(null)
    setAnchor(null)
    setSelectedBookingId(null)
  }

  // Drop both selections when the displayed week changes, by whatever means —
  // the buttons below, but just as much Back, a pasted link, or a parent that
  // re-resolves `?week=` for any other reason. A selection is slot indices
  // plus a date key, and carrying it across to a week that may not even share
  // that date would leave it pointing at nothing on screen. Keyed off the prop
  // itself, in render, rather than an effect on it or a clear inside the
  // button handlers below: this component cannot tell "the buttons changed
  // it" apart from "the caller changed it", and after task 5.8 it must not
  // need to.
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
   * Includes `resolvedConfig.timeZone` alongside `weekStart`: the fetch
   * window below is resolved through it, so a config swap (the placeholder
   * zone giving way to the Space's real one, before `weekStart` or either
   * token has changed) is a genuinely different request, not the same one
   * settling twice.
   */
  const requestKey = `${weekStart.getTime()}:${reloadNonce}:${refreshToken}:${resolvedConfig.timeZone}`
  const load: LoadState | { status: 'loading' } =
    settled !== null && settled.key === requestKey ? settled : { status: 'loading' }

  const slotsPerDay = slotsPerDayFor(resolvedConfig)
  const days = useMemo(() => daysOfWeek(weekStart), [weekStart])

  // ---- Loading the week's bookings -------------------------------------

  useEffect(() => {
    let cancelled = false
    // Midnight-to-midnight on the Space's own clock, not the environment's —
    // the same reason `bookingsByDay` below goes through `dayBounds` rather
    // than `weekStart` / `addDays` directly. A raw calendar-date instant
    // would ask the server for the wrong window whenever the Space's zone
    // isn't the environment's: a booking in the first few hours of the
    // Space's week could sit before an environment-midnight `from`, or one in
    // the last few hours could sit at or after an environment-midnight `to`,
    // and either way never reach this component to be drawn at all.
    const from = dayBounds(weekStart, resolvedConfig).start
    const to = dayBounds(addDays(weekStart, DAYS_PER_WEEK - 1), resolvedConfig).end

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
    // `weekStart` and `resolvedConfig` are read inside this effect only to
    // compute `from` / `to`, and `requestKey` already encodes both by value
    // (`weekStart.getTime()`, `resolvedConfig.timeZone`) — see its own
    // docblock. Listing the objects themselves here instead would re-fire
    // this fetch on a fresh-but-equal `Date` or config object, which is
    // exactly the hazard `ResourceCalendarPage`'s own `weekStart` stabiliser
    // exists to prevent one layer up; this effect must not reintroduce it
    // one layer down.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [publicId, requestKey, resourceId])

  /**
   * The bookings shown, per day.
   *
   * Empty while loading and on error — but note the grid does *not* then render
   * as a week of free slots: `blockedReason` treats both states as unselectable,
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
      const { start: dayStart, end: dayEnd } = dayBounds(day, resolvedConfig)
      return parsed.filter((entry) => intervalsOverlap(entry.start, entry.end, dayStart, dayEnd))
    })
  }, [bookings, days, resolvedConfig])

  // ---- What a user may click -------------------------------------------

  const blockedReason = useCallback(
    (dayIndex: number, index: number): SlotBlockedReason | null => {
      // Until the week's bookings are known, every slot is unselectable. The
      // alternative — an optimistically empty grid — invites a booking against
      // data we do not have.
      if (load.status !== 'ok') return 'unavailable'

      // Checked before the time-based reasons below: whether a slot sits
      // inside the Space's operating hours does not depend on `now`, only on
      // the config, and greying it is what replaces the old clipped grid —
      // the row still exists so a booking sitting on it stays visible.
      if (isSlotOutOfHours(index, resolvedConfig)) return 'out-of-hours'

      const day = days[dayIndex]
      const { start, end } = slotInterval(day, index, resolvedConfig)
      if (isSlotInPast(start, now)) return 'past'
      if (isSlotBeyondHorizon(start, now)) return 'beyond-horizon'

      const covering = bookingsByDay[dayIndex].some((entry) =>
        intervalsOverlap(start, end, entry.start, entry.end),
      )
      return covering ? 'booked' : null
    },
    [bookingsByDay, days, load.status, now, resolvedConfig],
  )

  // ---- Selection --------------------------------------------------------

  const selectedInterval = useMemo((): SelectedInterval | null => {
    if (selection === null) return null
    const dayIndex = days.findIndex((day) => toDateKey(day) === selection.dateKey)
    if (dayIndex === -1) return null
    return {
      start: slotInterval(days[dayIndex], selection.start, resolvedConfig).start,
      end: slotInterval(days[dayIndex], selection.end, resolvedConfig).end,
    }
  }, [days, resolvedConfig, selection])

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
    (dayIndex: number, index: number) => {
      if (anchor === null) return
      const day = days[dayIndex]
      // Selection is confined to the day it started on: a booking is one
      // interval, and slot indices on another column are a different day.
      if (toDateKey(day) !== anchor.dateKey) return

      const range = rangeBetween(anchor.index, index, (i) => blockedReason(dayIndex, i) === null)
      if (range === null) return
      setSelection({ dateKey: anchor.dateKey, ...range })
    },
    [anchor, blockedReason, days],
  )

  const beginAt = useCallback(
    (dayIndex: number, index: number) => {
      if (blockedReason(dayIndex, index) !== null) return
      const dateKey = toDateKey(days[dayIndex])
      setAnchor({ dateKey, index })
      setSelection({ dateKey, start: index, end: index })
      setSelectedBookingId(null)
    },
    [blockedReason, days],
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

  const canPrev = canGoToPreviousWeek(weekStart, now, resolvedConfig.timeZone)
  const canNext = canGoToNextWeek(weekStart, now, resolvedConfig.timeZone)
  const thisWeek = startOfWeek(zonedCalendarDate(now, resolvedConfig.timeZone))
  const isCurrentWeek = weekStart.getTime() === thisWeek.getTime()

  // Shown only when it differs from the Space's own zone (the module
  // docblock's "secondary hint, never a second version of the grid") — the
  // one place the viewer's own zone appears in this component at all.
  const viewerTimeZone = SYSTEM_TIME_ZONE
  const showViewerTimeZoneHint = viewerTimeZone !== resolvedConfig.timeZone

  /** Reports the week `deltaWeeks` away from the one currently shown. */
  const goToWeek = (deltaWeeks: number) => {
    onWeekChange?.(addDays(weekStart, deltaWeeks * DAYS_PER_WEEK))
  }

  /** Reports the current week — a no-op while it is already the one shown. */
  const goToThisWeek = () => {
    if (!isCurrentWeek) onWeekChange?.(thisWeek)
  }

  const dayHeight = slotsPerDay * SLOT_ROW_HEIGHT_PX
  const pxPerMinute = SLOT_ROW_HEIGHT_PX / resolvedConfig.slotMinutes

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
        Times shown in {resolvedConfig.timeZone}
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
        data-slots-per-day={slotsPerDay}
        className="flex select-none overflow-x-auto rounded border border-slate-200 bg-white"
      >
        {/* Time axis. One label per slot, so it stays aligned at any granularity. */}
        <div className="sticky left-0 z-10 shrink-0 border-r border-slate-200 bg-white">
          <div className="h-8 border-b border-slate-200" />
          {Array.from({ length: slotsPerDay }, (_, index) => (
            <div
              key={index}
              style={{ height: SLOT_ROW_HEIGHT_PX }}
              className="flex items-start justify-end px-2 text-[10px] leading-none text-slate-400 tabular-nums"
            >
              {formatSlotLabel(index, resolvedConfig)}
            </div>
          ))}
        </div>

        {days.map((day, dayIndex) => {
          const dateKey = toDateKey(day)
          // Midnight on the Space's own clock — see `bookingsByDay` above for
          // why this must not be the environment's midnight.
          const { start: dayStart } = dayBounds(day, resolvedConfig)

          return (
            <div
              key={dateKey}
              className="min-w-24 flex-1 border-r border-slate-200 last:border-r-0"
            >
              <div
                className="flex h-8 items-center justify-center border-b border-slate-200 text-xs font-medium text-slate-600"
                data-testid={`calendar-day-${dateKey}`}
              >
                {dayHeaderFormat.format(day)}
              </div>

              <div className="relative" style={{ height: dayHeight }}>
                {Array.from({ length: slotsPerDay }, (_, index) => {
                  const blocked = blockedReason(dayIndex, index)
                  const selected = isInSelection(selection, dateKey, index)

                  return (
                    <button
                      key={index}
                      type="button"
                      data-testid={slotTestId(day, index)}
                      data-blocked={blocked ?? undefined}
                      data-selected={selected || undefined}
                      aria-pressed={selected}
                      aria-label={`${dateKey} ${formatSlotLabel(index, resolvedConfig)}`}
                      disabled={blocked !== null}
                      style={{ height: SLOT_ROW_HEIGHT_PX }}
                      className={[
                        'block w-full border-b border-slate-100 text-left',
                        selected
                          ? 'bg-sky-500'
                          : blocked === null
                            ? 'hover:bg-sky-100'
                            : 'cursor-not-allowed bg-slate-100',
                      ].join(' ')}
                      onPointerDown={() => beginAt(dayIndex, index)}
                      onPointerOver={() => extendTo(dayIndex, index)}
                    />
                  )
                })}

                {bookingsByDay[dayIndex].map(({ booking, start, end }) => {
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
                          ? `Booked ${formatClockTime(start, resolvedConfig.timeZone)} to ${formatClockTime(end, resolvedConfig.timeZone)}`
                          : `Booked by someone else, ${formatClockTime(start, resolvedConfig.timeZone)} to ${formatClockTime(end, resolvedConfig.timeZone)}`
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
                      {formatClockTime(start, resolvedConfig.timeZone)}
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
          Selected {rangeLength(selection)} slot{rangeLength(selection) === 1 ? '' : 's'}:{' '}
          {formatZonedDateTime(selectedInterval.start, resolvedConfig.timeZone)} –{' '}
          {formatClockTime(selectedInterval.end, resolvedConfig.timeZone)}
        </p>
      )}
    </section>
  )
}
