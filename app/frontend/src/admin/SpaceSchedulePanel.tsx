import { useState } from 'react'

import { updateSpace, type Space } from '../api'
import { messageFor } from './messages'

/**
 * A short list of zones for the `<datalist>` — a convenience, not a validated
 * set. Any IANA name typed here is accepted or rejected by the server, which
 * is the only place `zoneinfo` actually knows what is valid.
 */
const COMMON_TIMEZONES = [
  'UTC',
  'Europe/Berlin',
  'Europe/London',
  'America/New_York',
  'America/Los_Angeles',
  'Asia/Tokyo',
  'Australia/Sydney',
  'Pacific/Auckland',
]

/**
 * `DEFERRED.md` item 2: the Space's timezone, operating hours, and slot
 * interval — the minimal surface something has to set them through.
 *
 * ## Configuration lives on the Space, not the Resource
 *
 * Task 4.13a moved operating hours and slot interval off `resources` and onto
 * `spaces`: a Resource is one of N indistinguishable courts and carries no
 * configuration of its own, so every court in a Space shares the one schedule
 * edited here. There is nothing left to configure per Resource, which is why
 * this panel — unlike its 4.12 predecessor — has no per-Resource rows at all.
 *
 * ## Rule parameters live here too
 *
 * Task 4.13b assembles each Space's rule canon from its own configuration
 * (`.claude/rules/rule-engine.md`), so `max_duration_minutes`,
 * `booking_horizon_days`, `max_bookings_per_week`, and
 * `max_bookings_per_month` are edited alongside the schedule fields above —
 * one Space-wide config, one panel. Each is nullable: an empty field clears
 * it to `null`, meaning that rule is not enforced for this Space, the same
 * "no restriction" convention the hours fields already use.
 *
 * ## Not a general configuration surface
 *
 * No Resource create/archive, no Space rename — those live elsewhere or
 * nowhere yet.
 *
 * ## Authorization
 *
 * Rendered only for an admin/owner by the caller (`AdminPage`'s `SpaceAdmin`),
 * the same convenience-not-boundary pattern every other panel here follows:
 * `update_space` is admin+ on the server, and the write below still handles
 * `forbidden` rather than assuming the hiding worked.
 */
export function SpaceSchedulePanel({
  space,
  onSpaceChanged,
}: {
  space: Space
  onSpaceChanged: (space: Space) => void
}) {
  const archived = space.archived_at !== null

  const [timezoneInput, setTimezoneInput] = useState(space.timezone)
  const [opensAt, setOpensAt] = useState(toTimeInputValue(space.opens_at))
  const [closesAt, setClosesAt] = useState(toTimeInputValue(space.closes_at))
  const [slotMinutes, setSlotMinutes] = useState(
    space.slot_minutes === null ? '' : String(space.slot_minutes),
  )
  const [maxDurationMinutes, setMaxDurationMinutes] = useState(
    toNumberInputValue(space.max_duration_minutes),
  )
  const [bookingHorizonDays, setBookingHorizonDays] = useState(
    toNumberInputValue(space.booking_horizon_days),
  )
  const [maxBookingsPerWeek, setMaxBookingsPerWeek] = useState(
    toNumberInputValue(space.max_bookings_per_week),
  )
  const [maxBookingsPerMonth, setMaxBookingsPerMonth] = useState(
    toNumberInputValue(space.max_bookings_per_month),
  )
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // The selected Space can change under this component (the picker in
  // AdminPage), and a save elsewhere can update the Space's schedule out from
  // under a stale local edit — both should reset the fields to what the
  // server now says. Done during render against a remembered value rather
  // than in an effect: the same pattern `BookingPanel` and `CalendarGrid`
  // use, and the one the `react-hooks/set-state-in-effect` lint enforces.
  const scheduleKey =
    `${space.public_id}:${space.timezone}:${space.opens_at}:` +
    `${space.closes_at}:${space.slot_minutes}:${space.max_duration_minutes}:` +
    `${space.booking_horizon_days}:${space.max_bookings_per_week}:` +
    `${space.max_bookings_per_month}`
  const [seenScheduleKey, setSeenScheduleKey] = useState(scheduleKey)
  if (seenScheduleKey !== scheduleKey) {
    setSeenScheduleKey(scheduleKey)
    setTimezoneInput(space.timezone)
    setOpensAt(toTimeInputValue(space.opens_at))
    setClosesAt(toTimeInputValue(space.closes_at))
    setSlotMinutes(space.slot_minutes === null ? '' : String(space.slot_minutes))
    setMaxDurationMinutes(toNumberInputValue(space.max_duration_minutes))
    setBookingHorizonDays(toNumberInputValue(space.booking_horizon_days))
    setMaxBookingsPerWeek(toNumberInputValue(space.max_bookings_per_week))
    setMaxBookingsPerMonth(toNumberInputValue(space.max_bookings_per_month))
    setError('')
  }

  async function handleSave() {
    setBusy(true)
    setError('')

    const result = await updateSpace(space.public_id, {
      timezone: timezoneInput,
      opens_at: fromTimeInputValue(opensAt),
      closes_at: fromTimeInputValue(closesAt),
      slot_minutes: slotMinutes === '' ? null : Number(slotMinutes),
      max_duration_minutes: fromNumberInputValue(maxDurationMinutes),
      booking_horizon_days: fromNumberInputValue(bookingHorizonDays),
      max_bookings_per_week: fromNumberInputValue(maxBookingsPerWeek),
      max_bookings_per_month: fromNumberInputValue(maxBookingsPerMonth),
    })
    setBusy(false)

    if (result.outcome === 'ok') {
      onSpaceChanged(result.data)
      return
    }

    setError(messageFor(result))
  }

  return (
    <section
      className="rounded-lg border border-slate-200 bg-white p-4"
      data-testid="space-schedule-panel"
    >
      <h2 className="text-sm font-semibold text-slate-900">Schedule</h2>
      <p className="mt-1 text-xs text-slate-500">
        The venue's timezone, operating hours, slot interval, and booking rules — shared
        by every Resource in this Space. Leave a rule field empty to leave it unenforced.
      </p>

      <div className="mt-3">
        <label className="block text-xs text-slate-600" htmlFor="space-timezone">
          Space timezone (IANA name, e.g. Europe/Berlin)
        </label>
        <input
          id="space-timezone"
          list="space-schedule-common-timezones"
          className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
          data-testid="timezone-input"
          value={timezoneInput}
          disabled={archived || busy}
          onChange={(event) => setTimezoneInput(event.target.value)}
        />
        <datalist id="space-schedule-common-timezones">
          {COMMON_TIMEZONES.map((zone) => (
            <option key={zone} value={zone} />
          ))}
        </datalist>
      </div>

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs text-slate-600" htmlFor="space-opens">
            Opens
          </label>
          <input
            id="space-opens"
            type="time"
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="opens-input"
            value={opensAt}
            disabled={archived || busy}
            onChange={(event) => setOpensAt(event.target.value)}
          />
        </div>

        <div>
          <label className="block text-xs text-slate-600" htmlFor="space-closes">
            Closes
          </label>
          <input
            id="space-closes"
            type="time"
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="closes-input"
            value={closesAt}
            disabled={archived || busy}
            onChange={(event) => setClosesAt(event.target.value)}
          />
        </div>

        <div>
          <label className="block text-xs text-slate-600" htmlFor="space-slot">
            Slot (minutes)
          </label>
          <input
            id="space-slot"
            type="number"
            min={1}
            className="mt-1 w-24 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="slot-input"
            value={slotMinutes}
            disabled={archived || busy}
            onChange={(event) => setSlotMinutes(event.target.value)}
          />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs text-slate-600" htmlFor="space-max-duration">
            Max booking length (minutes)
          </label>
          <input
            id="space-max-duration"
            type="number"
            min={1}
            className="mt-1 w-32 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="max-duration-input"
            value={maxDurationMinutes}
            disabled={archived || busy}
            onChange={(event) => setMaxDurationMinutes(event.target.value)}
          />
        </div>

        <div>
          <label className="block text-xs text-slate-600" htmlFor="space-horizon">
            Booking horizon (days)
          </label>
          <input
            id="space-horizon"
            type="number"
            min={1}
            className="mt-1 w-28 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="booking-horizon-input"
            value={bookingHorizonDays}
            disabled={archived || busy}
            onChange={(event) => setBookingHorizonDays(event.target.value)}
          />
        </div>

        <div>
          <label className="block text-xs text-slate-600" htmlFor="space-max-per-week">
            Max bookings per week
          </label>
          <input
            id="space-max-per-week"
            type="number"
            min={1}
            className="mt-1 w-28 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="max-bookings-per-week-input"
            value={maxBookingsPerWeek}
            disabled={archived || busy}
            onChange={(event) => setMaxBookingsPerWeek(event.target.value)}
          />
        </div>

        <div>
          <label className="block text-xs text-slate-600" htmlFor="space-max-per-month">
            Max bookings per month
          </label>
          <input
            id="space-max-per-month"
            type="number"
            min={1}
            className="mt-1 w-28 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="max-bookings-per-month-input"
            value={maxBookingsPerMonth}
            disabled={archived || busy}
            onChange={(event) => setMaxBookingsPerMonth(event.target.value)}
          />
        </div>

        <button
          type="button"
          className="shrink-0 rounded bg-slate-800 px-3 py-1 text-sm text-white disabled:opacity-50"
          data-testid="schedule-save"
          disabled={archived || busy}
          onClick={() => void handleSave()}
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>

      {archived ? (
        <p className="mt-2 text-sm text-slate-600" data-testid="schedule-archived">
          This Space is archived and can no longer be changed.
        </p>
      ) : error ? (
        <p className="mt-2 text-sm text-red-700" data-testid="schedule-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  )
}

/** `HH:MM:SS` (the wire shape) → `HH:MM` (what `<input type="time">` wants). */
function toTimeInputValue(value: string | null): string {
  return value === null ? '' : value.slice(0, 5)
}

/** `HH:MM` from the input, or `null` for "no restriction" → the wire shape. */
function fromTimeInputValue(value: string): string | null {
  return value === '' ? null : `${value}:00`
}

/** `number | null` (the wire shape) → the string a number input wants. */
function toNumberInputValue(value: number | null): string {
  return value === null ? '' : String(value)
}

/** The input's string value → `number | null`, empty meaning "not enforced". */
function fromNumberInputValue(value: string): number | null {
  return value === '' ? null : Number(value)
}
