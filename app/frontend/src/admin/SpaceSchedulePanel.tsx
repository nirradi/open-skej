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
 * `DEFERRED.md` item 2: the Space's timezone — the only property of its
 * schedule that stays a plain field rather than a rule instance.
 *
 * ## Configuration lives on the Space, not the Resource
 *
 * Task 4.13a moved operating hours and slot interval off `resources` and onto
 * `spaces`: a Resource is one of N indistinguishable courts and carries no
 * configuration of its own, so every court in a Space shares the one
 * configuration edited on this Space.
 *
 * ## Every booking constraint is a rule now, edited on its own page
 *
 * Task 6.8 moves operating hours, slot interval, max duration, booking
 * horizon, and the two frequency caps off this panel entirely: each is a
 * `space_rules` row now, created, scoped, paused, and deleted at
 * `/s/{public_id}/rules` (`SpaceRulesPage`). Keeping a second place to edit
 * the same configuration would leave two screens that could disagree about
 * what a Space enforces, so this panel no longer reads or writes any of
 * them — `timezone` is the one column left here because a venue is in one
 * physical place, not a rule instance about *when* it is open.
 *
 * ## Not a general configuration surface
 *
 * This panel itself still only edits the timezone. Resource create lives on
 * this same `/admin` page instead, as its own panel (`ResourcesPanel`) —
 * not here, because a Resource's identity has nothing to do with the venue's
 * clock. Space rename lives elsewhere or nowhere yet.
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
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // The selected Space can change under this component (the picker in
  // AdminPage), and a save elsewhere can update the Space's timezone out from
  // under a stale local edit — both should reset the field to what the
  // server now says. Done during render against a remembered value rather
  // than in an effect: the same pattern `BookingPanel` and `CalendarGrid`
  // use, and the one the `react-hooks/set-state-in-effect` lint enforces.
  const scheduleKey = `${space.public_id}:${space.timezone}`
  const [seenScheduleKey, setSeenScheduleKey] = useState(scheduleKey)
  if (seenScheduleKey !== scheduleKey) {
    setSeenScheduleKey(scheduleKey)
    setTimezoneInput(space.timezone)
    setError('')
  }

  async function handleSave() {
    setBusy(true)
    setError('')

    const result = await updateSpace(space.public_id, { timezone: timezoneInput })
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
        The venue's timezone — shared by every Resource in this Space. Operating hours, slot
        interval, and every booking rule are configured on the rules page instead.
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

      <button
        type="button"
        className="mt-3 shrink-0 rounded bg-slate-800 px-3 py-1 text-sm text-white disabled:opacity-50"
        data-testid="schedule-save"
        disabled={archived || busy}
        onClick={() => void handleSave()}
      >
        {busy ? 'Saving…' : 'Save'}
      </button>

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
