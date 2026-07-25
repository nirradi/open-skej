import { useEffect, useState } from 'react'

import { listResources, updateResource, updateSpace, type Resource, type Space } from '../api'
import { messageFor } from './messages'

type Load = { kind: 'resources'; resources: Resource[] } | { kind: 'error'; message: string } | null

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
 * `DEFERRED.md` item 2: the Space's timezone and each Resource's operating
 * hours and slot interval — the minimal surface something has to set them
 * through, now that 4.6 made hours per-Resource and 4.11 made the calendar
 * per-Resource.
 *
 * ## Not a general configuration surface
 *
 * No Resource create/archive here, no Space rename — those live elsewhere or
 * nowhere yet. No rule parameters (max duration, caps) — that is task 4.13,
 * and this panel does not reach into the rule engine at all.
 *
 * ## Authorization
 *
 * Rendered only for an admin/owner by the caller (`AdminPage`'s `SpaceAdmin`),
 * the same convenience-not-boundary pattern every other panel here follows:
 * `update_space` and `update_resource` are both admin+ on the server, and
 * every write below still handles `forbidden` rather than assuming the
 * hiding worked.
 */
export function ResourceConfigPanel({
  space,
  onSpaceChanged,
}: {
  space: Space
  onSpaceChanged: (space: Space) => void
}) {
  const archived = space.archived_at !== null

  const [timezoneInput, setTimezoneInput] = useState(space.timezone)
  const [timezoneError, setTimezoneError] = useState('')
  const [timezoneBusy, setTimezoneBusy] = useState(false)

  // The selected Space can change under this component (the picker in
  // AdminPage), and a save elsewhere can update `space.timezone` out from
  // under a stale local edit — both should reset the field to what the
  // server now says. Done during render against a remembered value rather
  // than in an effect: the same pattern `BookingPanel` and `CalendarGrid`
  // use, and the one the `react-hooks/set-state-in-effect` lint enforces.
  const timezoneKey = `${space.public_id}:${space.timezone}`
  const [seenTimezoneKey, setSeenTimezoneKey] = useState(timezoneKey)
  if (seenTimezoneKey !== timezoneKey) {
    setSeenTimezoneKey(timezoneKey)
    setTimezoneInput(space.timezone)
    setTimezoneError('')
  }

  async function handleTimezoneSave() {
    setTimezoneBusy(true)
    setTimezoneError('')

    const result = await updateSpace(space.public_id, { timezone: timezoneInput })
    setTimezoneBusy(false)

    if (result.outcome === 'ok') {
      onSpaceChanged(result.data)
      return
    }

    setTimezoneError(messageFor(result))
  }

  const [load, setLoad] = useState<Load>(null)

  useEffect(() => {
    let cancelled = false

    void listResources(space.public_id, { includeArchived: true }).then((result) => {
      if (cancelled) return
      setLoad(
        result.outcome === 'ok'
          ? { kind: 'resources', resources: result.data }
          : { kind: 'error', message: messageFor(result) },
      )
    })

    return () => {
      cancelled = true
    }
  }, [space.public_id])

  function handleResourceSaved(updated: Resource) {
    setLoad((current) =>
      current?.kind === 'resources'
        ? {
            kind: 'resources',
            resources: current.resources.map((resource) =>
              resource.id === updated.id ? updated : resource,
            ),
          }
        : current,
    )
  }

  return (
    <section
      className="rounded-lg border border-slate-200 bg-white p-4"
      data-testid="resource-config-panel"
    >
      <h2 className="text-sm font-semibold text-slate-900">Schedule</h2>
      <p className="mt-1 text-xs text-slate-500">
        The venue's timezone, and each Resource's operating hours and slot interval.
      </p>

      <div className="mt-3">
        <label className="block text-xs text-slate-600" htmlFor="space-timezone">
          Space timezone (IANA name, e.g. Europe/Berlin)
        </label>
        <div className="mt-1 flex items-center gap-2">
          <input
            id="space-timezone"
            list="resource-config-common-timezones"
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="timezone-input"
            value={timezoneInput}
            disabled={archived || timezoneBusy}
            onChange={(event) => setTimezoneInput(event.target.value)}
          />
          <datalist id="resource-config-common-timezones">
            {COMMON_TIMEZONES.map((zone) => (
              <option key={zone} value={zone} />
            ))}
          </datalist>
          <button
            type="button"
            className="shrink-0 rounded bg-slate-800 px-3 py-1 text-sm text-white disabled:opacity-50"
            data-testid="timezone-save"
            disabled={archived || timezoneBusy || timezoneInput === space.timezone}
            onClick={() => void handleTimezoneSave()}
          >
            {timezoneBusy ? 'Saving…' : 'Save'}
          </button>
        </div>

        {archived ? (
          <p className="mt-2 text-sm text-slate-600" data-testid="timezone-archived">
            This Space is archived and can no longer be changed.
          </p>
        ) : timezoneError ? (
          <p className="mt-2 text-sm text-red-700" data-testid="timezone-error" role="alert">
            {timezoneError}
          </p>
        ) : null}
      </div>

      <div className="mt-4 border-t border-slate-100 pt-4">
        <h3 className="text-xs font-semibold text-slate-700">Resources</h3>

        {load === null ? (
          <p className="mt-2 text-sm text-slate-600" data-testid="resources-loading" role="status">
            Loading Resources…
          </p>
        ) : load.kind === 'error' ? (
          <p className="mt-2 text-sm text-red-700" data-testid="resources-error" role="alert">
            {load.message}
          </p>
        ) : load.resources.length === 0 ? (
          // Not reachable in practice — creating a Space auto-creates one Resource —
          // but a list that renders nothing for an empty array looks broken.
          <p className="mt-2 text-sm text-slate-600" data-testid="resources-empty">
            This Space has no Resources.
          </p>
        ) : (
          <ul className="mt-3 divide-y divide-slate-100">
            {load.resources.map((resource) => (
              <ResourceRow
                key={resource.id}
                publicId={space.public_id}
                resource={resource}
                spaceArchived={archived}
                onSaved={handleResourceSaved}
              />
            ))}
          </ul>
        )}
      </div>
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

function ResourceRow({
  publicId,
  resource,
  spaceArchived,
  onSaved,
}: {
  publicId: string
  resource: Resource
  spaceArchived: boolean
  onSaved: (resource: Resource) => void
}) {
  const archived = spaceArchived || resource.archived_at !== null

  const [opensAt, setOpensAt] = useState(toTimeInputValue(resource.opens_at))
  const [closesAt, setClosesAt] = useState(toTimeInputValue(resource.closes_at))
  const [slotMinutes, setSlotMinutes] = useState(
    resource.slot_minutes === null ? '' : String(resource.slot_minutes),
  )
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // Reset the fields when the row's Resource — or its stored schedule — changes
  // out from under a stale local edit. Done during render against a remembered
  // signature rather than in an effect, matching `BookingPanel`/`CalendarGrid`
  // and the `react-hooks/set-state-in-effect` lint.
  const resourceKey = `${resource.id}:${resource.opens_at}:${resource.closes_at}:${resource.slot_minutes}`
  const [seenResourceKey, setSeenResourceKey] = useState(resourceKey)
  if (seenResourceKey !== resourceKey) {
    setSeenResourceKey(resourceKey)
    setOpensAt(toTimeInputValue(resource.opens_at))
    setClosesAt(toTimeInputValue(resource.closes_at))
    setSlotMinutes(resource.slot_minutes === null ? '' : String(resource.slot_minutes))
    setError('')
  }

  async function handleSave() {
    setBusy(true)
    setError('')

    const result = await updateResource(publicId, resource.id, {
      opens_at: fromTimeInputValue(opensAt),
      closes_at: fromTimeInputValue(closesAt),
      slot_minutes: slotMinutes === '' ? null : Number(slotMinutes),
    })
    setBusy(false)

    if (result.outcome === 'ok') {
      onSaved(result.data)
      return
    }

    setError(messageFor(result))
  }

  return (
    <li className="py-3" data-testid={`resource-row-${resource.id}`}>
      <p className="text-sm text-slate-900">
        {resource.name}
        {resource.archived_at !== null ? ' (archived)' : ''}
      </p>

      <div className="mt-2 flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs text-slate-600" htmlFor={`resource-opens-${resource.id}`}>
            Opens
          </label>
          <input
            id={`resource-opens-${resource.id}`}
            type="time"
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid={`resource-opens-${resource.id}`}
            value={opensAt}
            disabled={archived || busy}
            onChange={(event) => setOpensAt(event.target.value)}
          />
        </div>

        <div>
          <label className="block text-xs text-slate-600" htmlFor={`resource-closes-${resource.id}`}>
            Closes
          </label>
          <input
            id={`resource-closes-${resource.id}`}
            type="time"
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid={`resource-closes-${resource.id}`}
            value={closesAt}
            disabled={archived || busy}
            onChange={(event) => setClosesAt(event.target.value)}
          />
        </div>

        <div>
          <label className="block text-xs text-slate-600" htmlFor={`resource-slot-${resource.id}`}>
            Slot (minutes)
          </label>
          <input
            id={`resource-slot-${resource.id}`}
            type="number"
            min={1}
            className="mt-1 w-24 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid={`resource-slot-${resource.id}`}
            value={slotMinutes}
            disabled={archived || busy}
            onChange={(event) => setSlotMinutes(event.target.value)}
          />
        </div>

        <button
          type="button"
          className="rounded bg-slate-800 px-3 py-1 text-sm text-white disabled:opacity-50"
          data-testid={`resource-save-${resource.id}`}
          disabled={archived || busy}
          onClick={() => void handleSave()}
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>

      {archived && spaceArchived ? (
        <p className="mt-2 text-sm text-slate-600" data-testid={`resource-archived-${resource.id}`}>
          This Space is archived and can no longer be changed.
        </p>
      ) : resource.archived_at !== null ? (
        <p className="mt-2 text-sm text-slate-600" data-testid={`resource-archived-${resource.id}`}>
          This Resource is archived and can no longer be changed.
        </p>
      ) : error ? (
        <p className="mt-2 text-sm text-red-700" data-testid={`resource-error-${resource.id}`} role="alert">
          {error}
        </p>
      ) : null}
    </li>
  )
}
