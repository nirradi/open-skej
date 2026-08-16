import { useEffect, useState } from 'react'

import {
  archiveResource,
  createResource,
  listResources,
  updateResource,
  type Resource,
  type Space,
} from '../api'
import { messageFor } from './messages'

/** What the Resource list resolved to. `null` while the first load is in flight. */
type Load = { kind: 'resources'; resources: Resource[] } | { kind: 'error'; message: string } | null

/**
 * The Space's Resources — its bookable calendars — with a form to add one.
 *
 * ## Every Resource shares the Space's own configuration
 *
 * A Resource carries no hours, no slot interval, no rule parameters of its
 * own: it is one of N indistinguishable courts, a unit of bookable capacity.
 * The create form is therefore a single name field. There is deliberately no
 * parent, grouping, or type selector here — resource hierarchy is a later,
 * unrelated change (`.claude/rules/identity-and-access.md` says nothing
 * about one existing, and this stream does not add the concept).
 *
 * ## Archived Resources are shown, not hidden
 *
 * `listResources` is called with `includeArchived: true`, the opposite of
 * the member-facing default (`SpacePage`'s picker), because an admin
 * managing a venue needs to see what was retired as well as what is live.
 * An archived row is marked and offers no action — rename and retire are
 * both destructive-in-spirit-of-configuration, and a row already retired has
 * nothing left to change.
 *
 * ## Retire, never delete
 *
 * There is no `DELETE` for a Resource, and none is added here: bookings
 * carry a `resource_id`, so a row that vanished would take a booking's
 * meaning with it. Retiring calls `POST .../archive` and only stamps
 * `archived_at` — the row, and every booking against it, survives. The
 * control says "Retire" and never "Delete" or "Remove", and its confirmation
 * says exactly that: new bookings stop, existing ones are untouched. There
 * is no un-archive endpoint, so retiring is a one-way door and the
 * confirmation is a real one — the same two-step confirm-in-place
 * `SpaceRulesPage`'s `RuleRow` uses for its own delete, copied here rather
 * than `window.confirm`, which would block the E2E suite's automation.
 *
 * ## Authorization
 *
 * Rendered only for an admin/owner by the caller (`AdminPage`'s
 * `SpaceAdmin`), the same convenience-not-boundary pattern every panel here
 * follows: `create_resource` is admin+ on the server, and the outcome below
 * still handles `forbidden` rather than assuming the hiding worked.
 *
 * ## Archived Space
 *
 * `SpaceAdmin` already renders the archived banner above every panel; this
 * one additionally disables its own name field and submit button when
 * `space.archived_at !== null`, matching `SpaceSettingsPanel`'s
 * `settings-archived` notice — a panel that leaves an enabled control that
 * always 409s is a panel that fails silently.
 */
export function ResourcesPanel({
  space,
  onChanged,
}: {
  space: Space
  /** Called after a Resource is created, for a caller that needs to react. */
  onChanged: () => void
}) {
  const [load, setLoad] = useState<Load>(null)
  const [name, setName] = useState('')
  const [formError, setFormError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const archived = space.archived_at !== null

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

  async function handleCreate(event: React.FormEvent) {
    event.preventDefault()

    const trimmed = name.trim()
    if (trimmed === '') {
      setFormError('Enter a name for the Resource.')
      return
    }

    setSubmitting(true)
    setFormError('')

    const result = await createResource(space.public_id, { name: trimmed })
    setSubmitting(false)

    if (result.outcome === 'ok') {
      const created = result.data
      setLoad((current) =>
        current?.kind === 'resources'
          ? { kind: 'resources', resources: [...current.resources, created] }
          : { kind: 'resources', resources: [created] },
      )
      setName('')
      onChanged()
      return
    }

    setFormError(messageFor(result))
  }

  function handleResourceUpdated(updated: Resource) {
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
      data-testid="resources-panel"
    >
      <h2 className="text-sm font-semibold text-slate-900">Resources</h2>
      <p className="mt-1 text-xs text-slate-500">
        The bookable calendars in this Space. Every one shares the Space's own hours and rules.
      </p>

      <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={(e) => void handleCreate(e)}>
        <div className="min-w-0 flex-1">
          <label className="block text-xs text-slate-600" htmlFor="resource-name">
            Name
          </label>
          <input
            id="resource-name"
            type="text"
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="resource-name"
            value={name}
            disabled={archived || submitting}
            onChange={(event) => setName(event.target.value)}
          />
        </div>

        <button
          type="submit"
          className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
          data-testid="resource-create-submit"
          disabled={archived || submitting}
        >
          {submitting ? 'Adding…' : 'Add Resource'}
        </button>
      </form>

      {archived ? (
        <p className="mt-2 text-sm text-slate-600" data-testid="resources-archived">
          This Space is archived and can no longer be changed.
        </p>
      ) : formError ? (
        <p className="mt-2 text-sm text-red-700" data-testid="resource-create-error" role="alert">
          {formError}
        </p>
      ) : null}

      {load === null ? (
        <p className="mt-3 text-sm text-slate-600" data-testid="resources-loading" role="status">
          Loading Resources…
        </p>
      ) : load.kind === 'error' ? (
        <p className="mt-3 text-sm text-red-700" data-testid="resources-error" role="alert">
          {load.message}
        </p>
      ) : load.resources.length === 0 ? (
        <p className="mt-3 text-sm text-slate-600" data-testid="resources-empty">
          No Resources yet.
        </p>
      ) : (
        <ul className="mt-3 divide-y divide-slate-100">
          {load.resources.map((resource) => (
            <ResourceRow
              key={resource.id}
              space={space}
              resource={resource}
              disabled={archived}
              onUpdated={handleResourceUpdated}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

/**
 * One Resource: its name, an inline rename, and a retire control.
 *
 * `disabled` is the Space's own `archived_at !== null` — every control here
 * is disabled on an archived Space, matching the create form above. A
 * Resource that is itself archived offers no controls at all regardless of
 * `disabled`, since both endpoints reject an already-archived Resource with
 * `conflict` and there is nothing left here to do to it.
 */
function ResourceRow({
  space,
  resource,
  disabled,
  onUpdated,
}: {
  space: Space
  resource: Resource
  disabled: boolean
  onUpdated: (resource: Resource) => void
}) {
  const archived = resource.archived_at !== null

  const [editing, setEditing] = useState(false)
  const [nameDraft, setNameDraft] = useState(resource.name)
  const [confirmingRetire, setConfirmingRetire] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // The row's own stored name/archived_at can change out from under a local
  // edit — another admin's write, or this component's own successful save —
  // and the drafts should reset to match, the same "compare during render"
  // idiom `RuleRow` uses rather than an effect (the `set-state-in-effect`
  // lint this repo enforces).
  const rowKey = `${resource.id}:${resource.name}:${resource.archived_at}`
  const [seenRowKey, setSeenRowKey] = useState(rowKey)
  if (seenRowKey !== rowKey) {
    setSeenRowKey(rowKey)
    setNameDraft(resource.name)
    setEditing(false)
    setConfirmingRetire(false)
    setError('')
  }

  async function handleRename() {
    const trimmed = nameDraft.trim()
    if (trimmed === '') {
      setError('Enter a name for the Resource.')
      return
    }

    setBusy(true)
    setError('')

    const result = await updateResource(space.public_id, resource.id, { name: trimmed })
    setBusy(false)

    if (result.outcome === 'ok') {
      onUpdated(result.data)
      setEditing(false)
      return
    }

    setError(messageFor(result))
  }

  async function handleRetire() {
    setBusy(true)
    setError('')

    const result = await archiveResource(space.public_id, resource.id)
    setBusy(false)

    if (result.outcome === 'ok') {
      onUpdated(result.data)
      return
    }

    setError(messageFor(result))
  }

  return (
    <li className="py-2" data-testid={`resource-${resource.id}`}>
      <div className="flex items-center justify-between gap-2">
        {editing ? (
          <span
            className="flex min-w-0 flex-1 items-center gap-2"
            data-testid={`resource-${resource.id}-rename`}
          >
            <input
              type="text"
              className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid={`resource-${resource.id}-rename-input`}
              value={nameDraft}
              disabled={busy}
              onChange={(event) => setNameDraft(event.target.value)}
            />
            <button
              type="button"
              className="rounded bg-slate-900 px-2 py-1 text-sm text-white disabled:opacity-50"
              data-testid={`resource-${resource.id}-rename-save`}
              disabled={busy}
              onClick={() => void handleRename()}
            >
              {busy ? 'Saving…' : 'Save'}
            </button>
            <button
              type="button"
              className="rounded border border-slate-300 px-2 py-1 text-sm"
              data-testid={`resource-${resource.id}-rename-cancel`}
              disabled={busy}
              onClick={() => {
                setEditing(false)
                setNameDraft(resource.name)
                setError('')
              }}
            >
              Cancel
            </button>
          </span>
        ) : (
          <p
            className="truncate text-sm text-slate-900"
            data-testid={`resource-${resource.id}-name`}
          >
            {resource.name}
          </p>
        )}

        <div className="flex shrink-0 items-center gap-2">
          {archived ? (
            <span
              className="text-xs text-slate-500"
              data-testid={`resource-archived-${resource.id}`}
            >
              Retired
            </span>
          ) : editing ? null : (
            <>
              <button
                type="button"
                className="rounded border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
                data-testid={`resource-${resource.id}-rename-start`}
                disabled={disabled || busy}
                onClick={() => setEditing(true)}
              >
                Rename
              </button>

              {confirmingRetire ? (
                <span
                  className="flex items-center gap-2"
                  data-testid={`resource-${resource.id}-retire-confirm`}
                >
                  <button
                    type="button"
                    className="rounded bg-red-700 px-2 py-1 text-sm text-white disabled:opacity-50"
                    data-testid={`resource-${resource.id}-retire-confirm-yes`}
                    disabled={busy}
                    onClick={() => void handleRetire()}
                  >
                    {busy ? 'Retiring…' : 'Yes, retire'}
                  </button>
                  <button
                    type="button"
                    className="rounded border border-slate-300 px-2 py-1 text-sm"
                    data-testid={`resource-${resource.id}-retire-cancel`}
                    disabled={busy}
                    onClick={() => setConfirmingRetire(false)}
                  >
                    Cancel
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  className="rounded border border-red-300 px-2 py-1 text-sm text-red-700 disabled:opacity-50"
                  data-testid={`resource-${resource.id}-retire-start`}
                  disabled={disabled || busy}
                  onClick={() => setConfirmingRetire(true)}
                >
                  Retire
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {confirmingRetire ? (
        <p
          className="mt-1 text-xs text-slate-500"
          data-testid={`resource-${resource.id}-retire-notice`}
        >
          This Resource will stop taking new bookings. Its existing bookings are not affected.
        </p>
      ) : null}

      {error ? (
        <p
          className="mt-1 text-sm text-red-700"
          data-testid={`resource-${resource.id}-error`}
          role="alert"
        >
          {error}
        </p>
      ) : null}
    </li>
  )
}
