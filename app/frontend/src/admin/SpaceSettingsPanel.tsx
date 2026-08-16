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
 * `DEFERRED.md` item 2 named the timezone; task 9.5 adds the other two plain
 * columns `PATCH /spaces/{public_id}` writes.
 *
 * ## The three columns travel together
 *
 * `name`, `description` and `timezone` are the whole of what a Space's own
 * row carries beyond its id — everything else a venue enforces is a
 * `space_rules` instance (`identity-and-access.md`). All three are one
 * `PATCH`, so one save writing all three is simpler than a second panel that
 * would race this one over the same row: this panel already resets its
 * fields from the server during render against a remembered key precisely
 * because a save elsewhere can change the Space underneath it, and a second
 * panel editing `name` would need the identical guard.
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
 * what a Space enforces, so this panel still does not read or write any of
 * them.
 *
 * ## Not a general configuration surface
 *
 * This panel edits `name`, `description` and `timezone` and nothing past
 * them. Resource create/rename/retire lives on this same `/admin` page
 * instead, as its own panel (`ResourcesPanel`) — not here, because a
 * Resource's identity has nothing to do with the venue's own name or clock.
 * The Space's `public_id` is never editable here or anywhere: it is the
 * entire distribution model, and there is no endpoint to change it.
 *
 * ## Authorization
 *
 * Rendered only for an admin/owner by the caller (`AdminPage`'s `SpaceAdmin`),
 * the same convenience-not-boundary pattern every other panel here follows:
 * `update_space` is admin+ on the server, and the write below still handles
 * `forbidden` rather than assuming the hiding worked.
 */
export function SpaceSettingsPanel({
  space,
  onSpaceChanged,
}: {
  space: Space
  onSpaceChanged: (space: Space) => void
}) {
  const archived = space.archived_at !== null

  const [nameInput, setNameInput] = useState(space.name)
  const [descriptionInput, setDescriptionInput] = useState(space.description ?? '')
  const [timezoneInput, setTimezoneInput] = useState(space.timezone)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // The selected Space can change under this component (the picker in
  // AdminPage), and a save elsewhere can update the Space out from under a
  // stale local edit — both should reset every field to what the server now
  // says. Done during render against a remembered value rather than in an
  // effect: the same pattern `BookingPanel` and `CalendarGrid` use, and the
  // one the `react-hooks/set-state-in-effect` lint enforces.
  const settingsKey = `${space.public_id}:${space.name}:${space.description}:${space.timezone}`
  const [seenSettingsKey, setSeenSettingsKey] = useState(settingsKey)
  if (seenSettingsKey !== settingsKey) {
    setSeenSettingsKey(settingsKey)
    setNameInput(space.name)
    setDescriptionInput(space.description ?? '')
    setTimezoneInput(space.timezone)
    setError('')
  }

  const trimmedName = nameInput.trim()

  async function handleSave() {
    if (trimmedName === '') {
      setError('Name is required.')
      return
    }

    setBusy(true)
    setError('')

    // An empty description box means "clear it", which the server takes as
    // `null` rather than `""` — `updateSpace`'s own docstring states the
    // omitted-vs-null distinction and this is the one call site that has to
    // honour it. `name` and `timezone` are never nullable, so they always go
    // over as the (non-empty) string in the box.
    const result = await updateSpace(space.public_id, {
      name: trimmedName,
      description: descriptionInput.trim() === '' ? null : descriptionInput,
      timezone: timezoneInput,
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
      data-testid="space-settings-panel"
    >
      <h2 className="text-sm font-semibold text-slate-900">Settings</h2>
      <p className="mt-1 text-xs text-slate-500">
        The venue's name, description and timezone. Operating hours, slot interval, and every
        booking rule are configured on the rules page instead.
      </p>

      <div className="mt-3">
        <label className="block text-xs text-slate-600" htmlFor="space-name">
          Name
        </label>
        <input
          id="space-name"
          className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
          data-testid="name-input"
          value={nameInput}
          disabled={archived || busy}
          onChange={(event) => setNameInput(event.target.value)}
        />
      </div>

      <div className="mt-3">
        <label className="block text-xs text-slate-600" htmlFor="space-description">
          Description
        </label>
        <textarea
          id="space-description"
          className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
          data-testid="description-input"
          value={descriptionInput}
          disabled={archived || busy}
          onChange={(event) => setDescriptionInput(event.target.value)}
        />
      </div>

      <div className="mt-3">
        <label className="block text-xs text-slate-600" htmlFor="space-timezone">
          Space timezone (IANA name, e.g. Europe/Berlin)
        </label>
        <input
          id="space-timezone"
          list="space-settings-common-timezones"
          className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
          data-testid="timezone-input"
          value={timezoneInput}
          disabled={archived || busy}
          onChange={(event) => setTimezoneInput(event.target.value)}
        />
        <datalist id="space-settings-common-timezones">
          {COMMON_TIMEZONES.map((zone) => (
            <option key={zone} value={zone} />
          ))}
        </datalist>
      </div>

      <button
        type="button"
        className="mt-3 shrink-0 rounded bg-slate-800 px-3 py-1 text-sm text-white disabled:opacity-50"
        data-testid="settings-save"
        disabled={archived || busy}
        onClick={() => void handleSave()}
      >
        {busy ? 'Saving…' : 'Save'}
      </button>

      {archived ? (
        <p className="mt-2 text-sm text-slate-600" data-testid="settings-archived">
          This Space is archived and can no longer be changed.
        </p>
      ) : error ? (
        <p className="mt-2 text-sm text-red-700" data-testid="settings-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  )
}
