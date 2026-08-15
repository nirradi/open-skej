import { useEffect, useState } from 'react'

import { createResource, listResources, type Resource, type Space } from '../api'
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
 * An archived row is marked and offers no action — there is nothing to
 * offer yet; rename and retire arrive in a later task.
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
 * `space.archived_at !== null`, matching `SpaceSchedulePanel`'s
 * `schedule-archived` notice — a panel that leaves an enabled control that
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
            <li key={resource.id} className="py-2" data-testid={`resource-${resource.id}`}>
              <div className="flex items-center justify-between gap-2">
                <p className="truncate text-sm text-slate-900">{resource.name}</p>
                {resource.archived_at !== null ? (
                  <span
                    className="text-xs text-slate-500"
                    data-testid={`resource-archived-${resource.id}`}
                  >
                    Archived
                  </span>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
