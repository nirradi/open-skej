/**
 * The confirm-and-cancel step for an existing booking.
 *
 * ## Why the confirmation is rendered, not `window.confirm`
 *
 * A native modal is untestable without stubbing a global, unstyleable, and
 * blocks the whole tab. The confirmation here is a second render state of the
 * same panel, so it is addressable by the same test ids as everything else and
 * the in-flight state can disable it.
 *
 * ## Why each outcome maps where it does
 *
 * `cancelResourceBooking` returns several outcomes, and — unlike booking —
 * most of them mean the user got what they asked for:
 *
 * - **`ok`** — the cancel landed. Refresh: the refetch is what frees the slot
 *   for rebooking without a page reload.
 * - **`already_cancelled`** — a 409, the same status as `overlap`, and the trap
 *   this panel exists to avoid. It does *not* mean somebody collided with the
 *   user; it means the user's own cancel already landed, typically because the
 *   button was double-clicked. The end state they wanted holds, so this renders
 *   as **success**, identically to `ok`, and refreshes the same way. Showing it
 *   as a conflict would invent a problem out of the desired outcome. The copy is
 *   deliberately the same as `ok`'s: the booking is cancelled either way, and
 *   the distinction is ours, not theirs.
 * - **`not_found`** — the booking is gone (or the Space/Resource is not the
 *   caller's — the two are indistinguishable on purpose, see
 *   `ListResourceBookingsResult`'s docstring). Also not a failure worth
 *   alarming anyone about: the block on screen was stale, so this refreshes to
 *   make it disappear and says so plainly, in a neutral notice rather than an
 *   error.
 * - **`already_started`** — the interval is already under way, so there is no
 *   remedy at all: retrying will refuse the same way every time. Rendered as a
 *   plain, terminal statement, and the confirm control is hidden rather than
 *   inviting a retry — unlike `failed`, where trying again might work.
 * - **`not_yours`** — a plain member trying to cancel someone else's booking.
 *   Terminal in the same way as `already_started`: retrying refuses identically
 *   every time, since nothing this panel can do changes whose booking it is, so
 *   the confirm control is hidden rather than offered again. Distinct from
 *   `forbidden` even though both are 403 — this one names a rule about *this*
 *   cancellation specifically, and the server's own copy says so, so it is
 *   shown rather than folded into the generic access-floor failure. The button
 *   that produced it stays visible on other bookings: 5.3, not this task, is
 *   what stops the calendar from offering a cancel it knows the server will
 *   refuse.
 * - **`unauthenticated` / `forbidden`** — the access floor every authenticated
 *   route carries. Neither is a rule about *this* cancellation, so both render
 *   as the same generic failure `failed` does.
 * - **`invalid_request`** — our bug. `detail` is Pydantic's diagnostics, logged
 *   and never rendered, exactly as in `BookingPanel`.
 * - **`failed`** — network or server. Generic copy, nothing to act on.
 *
 * The union makes this exhaustive: adding an outcome in `types.ts` without
 * handling it here fails to compile.
 */

import { useCallback, useRef, useState } from 'react'

import type { Booking } from '../api'
import { cancelResourceBooking } from '../api'
import { summariseInterval } from './summary'

/** What the panel is currently showing below its controls. */
export type CancelResult =
  | { kind: 'idle' }
  | { kind: 'cancelling' }
  /** The booking is cancelled — whether we did it now or had already done it. */
  | { kind: 'success'; message: string }
  /** Nothing to do, and nothing wrong: neutral, not alarming. */
  | { kind: 'notice'; message: string }
  /** The interval is already under way: terminal, no retry offered. */
  | { kind: 'started'; message: string }
  /** Someone else's booking: terminal, no retry offered. */
  | { kind: 'not_yours'; message: string }
  /** Our bug, the access floor, or an unactionable failure: generic copy only. */
  | { kind: 'error'; message: string }

const SUCCESS_MESSAGE = 'Cancelled. The slot is free again.'

const NOT_FOUND_MESSAGE =
  "That booking is no longer on the calendar, so there was nothing to cancel. We've refreshed the week."

const CLIENT_BUG_MESSAGE =
  "Something went wrong preparing that cancellation, so it wasn't sent. Please try again."

export interface CancelPanelProps {
  /** The Space the Resource belongs to. */
  publicId: string
  /** The Resource the booking belongs to. */
  resourceId: number
  /** The booking to cancel, or `null` when none is selected. */
  booking: Booking | null
  /** Called after a change the calendar must reflect (any settled cancellation). */
  onCalendarChanged: () => void
}

export function CancelPanel({ publicId, resourceId, booking, onCalendarChanged }: CancelPanelProps) {
  const [result, setResult] = useState<CancelResult>({ kind: 'idle' })
  const [confirming, setConfirming] = useState(false)

  // Identifies the booking a result belongs to, mirroring `BookingPanel`.
  //
  // A *null* booking deliberately does not clear it. A successful cancel drops
  // the selection as a side effect — the block it named has just stopped
  // existing — so treating that as "the user moved on" would erase the
  // confirmation in the same tick it was set, and the cancellation would land
  // with no visible feedback at all. That is the exact bug `App.test.tsx` was
  // written for on the booking side. Only selecting a *different* booking
  // discards the result.
  const bookingKey = booking === null ? null : booking.id
  const [resultKey, setResultKey] = useState<number | null>(null)
  if (bookingKey !== null && resultKey !== bookingKey) {
    setResultKey(bookingKey)
    if (confirming) setConfirming(false)
    if (result.kind !== 'idle' && result.kind !== 'cancelling') setResult({ kind: 'idle' })
  }

  // Guards the double click that `disabled` cannot: two clicks dispatched
  // before React re-renders both read the pre-disable DOM, and a duplicate
  // DELETE is exactly what produces the `already_cancelled` this panel then has
  // to explain away. A ref settles synchronously, so the second click loses.
  const inFlight = useRef(false)

  const confirm = useCallback(async () => {
    if (booking === null || inFlight.current) return
    inFlight.current = true
    setResult({ kind: 'cancelling' })

    try {
      const outcome = await cancelResourceBooking(publicId, resourceId, booking.id)

      switch (outcome.outcome) {
        case 'ok':
          setResult({ kind: 'success', message: SUCCESS_MESSAGE })
          // The refetch is what makes the freed slot bookable again.
          onCalendarChanged()
          break
        case 'already_cancelled':
          // Success, not a conflict. See the note at the top of this file.
          setResult({ kind: 'success', message: SUCCESS_MESSAGE })
          onCalendarChanged()
          break
        case 'not_found':
          setResult({ kind: 'notice', message: NOT_FOUND_MESSAGE })
          // The block on screen is stale; refreshing is what removes it.
          onCalendarChanged()
          break
        case 'already_started':
          // No remedy: the interval is under way and retrying refuses the
          // same way every time, so the confirm control is hidden rather than
          // offered again.
          setResult({ kind: 'started', message: outcome.message })
          break
        case 'not_yours':
          // No remedy from here either: the caller cannot make this booking
          // theirs, so the confirm control is hidden the same way it is for
          // `already_started`.
          setResult({ kind: 'not_yours', message: outcome.message })
          break
        case 'invalid_request':
          // `detail` is diagnostics, not copy. Logged, never rendered.
          console.error('cancelResourceBooking rejected the request', outcome.detail, outcome.raw)
          setResult({ kind: 'error', message: CLIENT_BUG_MESSAGE })
          break
        case 'unauthenticated':
        case 'forbidden':
        case 'failed':
          setResult({ kind: 'error', message: outcome.message })
          break
      }
    } finally {
      inFlight.current = false
      setConfirming(false)
    }
  }, [booking, onCalendarChanged, publicId, resourceId])

  /** The outcome banner, rendered whether or not a booking survives it. */
  const banner = (
    <>
      {result.kind === 'success' && (
        <p
          role="status"
          data-testid="cancel-success"
          className="rounded border border-emerald-200 bg-emerald-50 p-3 text-emerald-800"
        >
          {result.message}
        </p>
      )}

      {result.kind === 'notice' && (
        <p
          role="status"
          data-testid="cancel-notice"
          className="rounded border border-sky-200 bg-sky-50 p-3 text-sky-900"
        >
          {result.message}
        </p>
      )}

      {result.kind === 'started' && (
        <p
          role="alert"
          data-testid="cancel-started"
          className="rounded border border-slate-300 bg-slate-100 p-3 text-slate-700"
        >
          {result.message}
        </p>
      )}

      {result.kind === 'not_yours' && (
        <p
          role="alert"
          data-testid="cancel-not-yours"
          className="rounded border border-slate-300 bg-slate-100 p-3 text-slate-700"
        >
          {result.message}
        </p>
      )}

      {result.kind === 'error' && (
        <p
          role="alert"
          data-testid="cancel-error"
          className="rounded border border-slate-300 bg-slate-100 p-3 text-slate-700"
        >
          {result.message}
        </p>
      )}
    </>
  )

  // Nothing selected and nothing to report: the panel stays out of the way
  // rather than occupying the column with an empty prompt, which the booking
  // panel beside it is already providing.
  if (booking === null && result.kind === 'idle') return null

  if (booking === null) {
    return (
      <aside
        className="rounded-lg border border-slate-200 bg-white p-4 text-sm"
        data-testid="cancel-panel"
      >
        {banner}
      </aside>
    )
  }

  const summary = summariseInterval({
    start: new Date(booking.start_at),
    end: new Date(booking.end_at),
  })
  const cancelling = result.kind === 'cancelling'

  return (
    <aside
      className="rounded-lg border border-slate-200 bg-white p-4 text-sm"
      data-testid="cancel-panel"
    >
      <h2 className="text-base font-semibold text-slate-900">Your booking</h2>

      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-slate-700">
        <dt className="text-slate-500">Day</dt>
        <dd data-testid="cancel-day">{summary.day}</dd>
        <dt className="text-slate-500">Time</dt>
        <dd data-testid="cancel-time">
          {summary.start} – {summary.end}
        </dd>
        <dt className="text-slate-500">Duration</dt>
        <dd data-testid="cancel-duration">{summary.duration}</dd>
      </dl>

      {!confirming && !cancelling && result.kind !== 'started' && result.kind !== 'not_yours' && (
        <button
          type="button"
          data-testid="cancel-start"
          onClick={() => setConfirming(true)}
          className="mt-4 rounded border border-rose-300 px-4 py-2 font-medium text-rose-700 hover:bg-rose-50"
        >
          Cancel this booking
        </button>
      )}

      {(confirming || cancelling) && (
        <div className="mt-4" data-testid="cancel-confirming">
          <p className="text-slate-700">
            Cancel this booking? The slot goes back on the calendar for anyone to take.
          </p>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              data-testid="cancel-confirm"
              onClick={() => void confirm()}
              disabled={cancelling}
              className="rounded bg-rose-600 px-4 py-2 font-medium text-white hover:bg-rose-500 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {cancelling ? 'Cancelling…' : 'Yes, cancel it'}
            </button>
            <button
              type="button"
              data-testid="cancel-keep"
              onClick={() => setConfirming(false)}
              disabled={cancelling}
              className="rounded border border-slate-300 px-4 py-2 font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Keep it
            </button>
          </div>
        </div>
      )}

      {result.kind !== 'idle' && result.kind !== 'cancelling' && (
        <div className="mt-3">{banner}</div>
      )}
    </aside>
  )
}
