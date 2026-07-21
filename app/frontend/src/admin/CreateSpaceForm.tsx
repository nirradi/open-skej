import { useState } from 'react'

import { createSpace, type Space } from '../api'
import { messageFor } from './messages'
import { ShareLink } from './ShareLink'

/**
 * Create a Space, then show its share link.
 *
 * Available to every signed-in user, not just admins: anyone may create a Space
 * and becomes its owner. There is no global superuser, which is what keeps two
 * tenants on one deployment genuinely independent.
 *
 * ## The link is shown immediately, and that is the point
 *
 * `public_id` is the only handle to a new Space that will ever exist — nothing
 * enumerates Spaces, and there is no lookup by name. The creator keeps access
 * through their membership, but the *link*, the thing that lets them bring
 * anyone else in, appears exactly once in the create response. Surfacing it here
 * with a copy button is what stops a freshly created Space from being one the
 * owner cannot share.
 */
export function CreateSpaceForm({ onCreated }: { onCreated: (space: Space) => void }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [created, setCreated] = useState<Space | null>(null)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()

    const trimmed = name.trim()
    if (trimmed === '') {
      setError('Give the Space a name.')
      return
    }

    setSubmitting(true)
    setError('')

    const result = await createSpace(trimmed, description.trim() || null)
    setSubmitting(false)

    if (result.outcome === 'ok') {
      setCreated(result.data)
      setName('')
      setDescription('')
      onCreated(result.data)
      return
    }

    setError(messageFor(result))
  }

  return (
    <section
      className="rounded-lg border border-slate-200 bg-white p-4"
      data-testid="create-space-panel"
    >
      <h2 className="text-sm font-semibold text-slate-900">Create a Space</h2>

      <form className="mt-3 space-y-3" onSubmit={(event) => void handleSubmit(event)}>
        <div>
          <label className="block text-xs text-slate-600" htmlFor="space-name">
            Name
          </label>
          <input
            id="space-name"
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="space-name"
            value={name}
            disabled={submitting}
            onChange={(event) => setName(event.target.value)}
          />
        </div>

        <div>
          <label className="block text-xs text-slate-600" htmlFor="space-description">
            Description <span className="text-slate-400">(optional)</span>
          </label>
          <input
            id="space-description"
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="space-description"
            value={description}
            disabled={submitting}
            onChange={(event) => setDescription(event.target.value)}
          />
        </div>

        <button
          type="submit"
          className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
          data-testid="space-create-submit"
          disabled={submitting}
        >
          {submitting ? 'Creating…' : 'Create Space'}
        </button>
      </form>

      {error ? (
        <p className="mt-2 text-sm text-red-700" data-testid="create-space-error" role="alert">
          {error}
        </p>
      ) : null}

      {created ? (
        <div className="mt-4 rounded border border-slate-200 bg-slate-50 p-3" data-testid="created-space">
          <p className="text-sm text-slate-900">
            Created <strong>{created.name}</strong>. Share this link with anyone who should join.
          </p>
          <ShareLink publicId={created.public_id} />
        </div>
      ) : null}
    </section>
  )
}
