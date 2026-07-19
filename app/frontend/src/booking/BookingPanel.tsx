/**
 * The confirm-and-submit step for a selected range.
 *
 * ## Why each outcome gets its own state
 *
 * `createBooking` returns a discriminated union, and the four ways it can fail
 * are four genuinely different situations for the person looking at the screen:
 *
 * - **`rule_denied`** — the request broke a booking rule. The engine wrote copy
 *   for exactly this moment, so it is rendered **verbatim**. Paraphrasing it, or
 *   wrapping it in a generic "Error:" prefix, throws away the one message that
 *   tells the user what to change. The fix is theirs to make, so the selection
 *   stays put and they can adjust it.
 * - **`overlap`** — the rules were fine; somebody else took the slot. That means
 *   the grid on screen is *stale*, so this is the only branch that refreshes the
 *   week. Showing it identically to a denial would tell the user to change a
 *   request that was never the problem.
 * - **`invalid_request`** — the client sent something the server could not parse.
 *   That is our bug, not theirs. `detail` is developer diagnostics and is logged,
 *   never rendered: it is where Pydantic's internals would otherwise leak into
 *   the UI.
 * - **`failed`** — the network or the server broke. Generic copy, nothing to act on.
 *
 * The union makes this exhaustive: adding an outcome in `types.ts` without
 * handling it here fails to compile.
 */

import { useCallback, useState } from 'react'

import type { SelectedInterval } from '../calendar'
import { createBooking } from '../api'
import { summariseInterval } from './summary'

/** What the panel is currently showing below the confirm button. */
export type PanelResult =
  | { kind: 'idle' }
  | { kind: 'submitting' }
  | { kind: 'success'; message: string }
  /** The rule engine's own copy, rendered verbatim. */
  | { kind: 'denied'; message: string }
  /** Someone else holds the slot; the calendar has been refreshed. */
  | { kind: 'conflict'; message: string }
  /** Our bug or theirs-but-unactionable: generic copy only. */
  | { kind: 'error'; message: string }

const CLIENT_BUG_MESSAGE =
  "Something went wrong preparing that booking, so it wasn't saved. Please try again."

const SUCCESS_MESSAGE = 'Booked. Your reservation is on the calendar.'

export interface BookingPanelProps {
  /** The range to book, or `null` when nothing is selected. */
  selection: SelectedInterval | null
  /** Called after a change that the calendar must reflect (a booking, or a conflict). */
  onCalendarChanged: () => void
}

export function BookingPanel({ selection, onCalendarChanged }: BookingPanelProps) {
  const [result, setResult] = useState<PanelResult>({ kind: 'idle' })

  // Identifies the selection a result belongs to. A result about a range the
  // user has since moved off is noise, so it is dropped during render rather
  // than cleared by an effect keyed on `selection`.
  //
  // A *null* selection deliberately does not clear it. Booking successfully
  // clears the selection as a side effect — the slots just became unbookable —
  // so treating that as "the user moved on" would erase the confirmation in the
  // same tick it was set, and the booking would land with no visible feedback
  // at all. Only moving to a different concrete range discards the result.
  const selectionKey =
    selection === null ? null : `${selection.start.getTime()}-${selection.end.getTime()}`
  const [resultKey, setResultKey] = useState<string | null>(null)
  if (selectionKey !== null && resultKey !== selectionKey && result.kind !== 'submitting') {
    setResultKey(selectionKey)
    if (result.kind !== 'idle') setResult({ kind: 'idle' })
  }

  /** The outcome banner, rendered whether or not a selection survives it. */
  const banner = (
    <>
      {result.kind === 'success' && (
        <p
          role="status"
          data-testid="booking-success"
          className="mt-3 rounded border border-emerald-200 bg-emerald-50 p-3 text-emerald-800"
        >
          {result.message}
        </p>
      )}

      {result.kind === 'denied' && (
        <p
          role="alert"
          data-testid="booking-denied"
          className="mt-3 rounded border border-amber-200 bg-amber-50 p-3 text-amber-900"
        >
          {result.message}
        </p>
      )}

      {result.kind === 'conflict' && (
        <p
          role="alert"
          data-testid="booking-conflict"
          className="mt-3 rounded border border-rose-200 bg-rose-50 p-3 text-rose-900"
        >
          {result.message}
        </p>
      )}

      {result.kind === 'error' && (
        <p
          role="alert"
          data-testid="booking-error"
          className="mt-3 rounded border border-slate-300 bg-slate-100 p-3 text-slate-700"
        >
          {result.message}
        </p>
      )}
    </>
  )

  const submit = useCallback(async () => {
    if (selection === null) return
    setResult({ kind: 'submitting' })

    const outcome = await createBooking(selection.start, selection.end)

    switch (outcome.outcome) {
      case 'ok':
        setResult({ kind: 'success', message: SUCCESS_MESSAGE })
        // Refetch so the new booking is drawn from what the server actually
        // holds, rather than from an optimistic copy that could drift.
        onCalendarChanged()
        break
      case 'rule_denied':
        // Verbatim: this copy was written for the user by the rule engine.
        setResult({ kind: 'denied', message: outcome.message })
        break
      case 'overlap':
        setResult({ kind: 'conflict', message: outcome.message })
        // The week on screen no longer matches reality — pull the booking that
        // beat us so the user can see the slot is genuinely taken.
        onCalendarChanged()
        break
      case 'invalid_request':
        // `detail` is diagnostics, not copy. Logged, never rendered.
        console.error('createBooking rejected the request', outcome.detail, outcome.raw)
        setResult({ kind: 'error', message: CLIENT_BUG_MESSAGE })
        break
      case 'failed':
        setResult({ kind: 'error', message: outcome.message })
        break
    }
  }, [onCalendarChanged, selection])

  if (selection === null) {
    return (
      <aside
        className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-600"
        data-testid="booking-panel"
      >
        <p data-testid="booking-empty">
          Select a time on the calendar to book it. Drag across slots for a longer booking.
        </p>
        {banner}
      </aside>
    )
  }

  const summary = summariseInterval(selection)
  const submitting = result.kind === 'submitting'

  return (
    <aside
      className="rounded-lg border border-slate-200 bg-white p-4 text-sm"
      data-testid="booking-panel"
    >
      <h2 className="text-base font-semibold text-slate-900">Confirm your booking</h2>

      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-slate-700">
        <dt className="text-slate-500">Day</dt>
        <dd data-testid="booking-day">{summary.day}</dd>
        <dt className="text-slate-500">Time</dt>
        <dd data-testid="booking-time">
          {summary.start} – {summary.end}
        </dd>
        <dt className="text-slate-500">Duration</dt>
        <dd data-testid="booking-duration">{summary.duration}</dd>
      </dl>

      <button
        type="button"
        data-testid="booking-confirm"
        onClick={() => void submit()}
        disabled={submitting}
        className="mt-4 rounded bg-indigo-600 px-4 py-2 font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-slate-300"
      >
        {submitting ? 'Booking…' : 'Book'}
      </button>

      {banner}
    </aside>
  )
}
