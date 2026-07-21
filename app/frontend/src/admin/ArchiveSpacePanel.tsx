import { useState } from 'react'

import { archiveSpace, type Space } from '../api'
import { messageFor } from './messages'

/**
 * Archiving a Space, behind a two-step confirmation. **Owner only.**
 *
 * ## Why a confirmation at all
 *
 * There is no un-archive endpoint. Archiving is the one action in this dashboard
 * with no inverse, and an archived Space refuses every subsequent mutation — so
 * a misplaced click is not recoverable through the UI, or through the API at
 * all. That asymmetry is the whole argument for the extra step: everything else
 * here can be undone by doing the opposite thing.
 *
 * The confirm state is local and starts collapsed, so **cancelling archives
 * nothing** — no request is issued until the second button is pressed. That is
 * worth stating because the tempting implementation, a `window.confirm` around
 * the call, is untestable in jsdom without stubbing a global and tends to get
 * stubbed to `true` in tests, which quietly removes the guard from every
 * assertion that touches it.
 */
export function ArchiveSpacePanel({
  space,
  onArchived,
}: {
  space: Space
  onArchived: (archived: Space) => void
}) {
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  if (space.archived_at !== null) {
    return (
      <section
        className="rounded-lg border border-slate-200 bg-white p-4"
        data-testid="archive-panel"
      >
        <h2 className="text-sm font-semibold text-slate-900">Archive</h2>
        <p className="mt-2 text-sm text-slate-600" data-testid="archive-already">
          This Space is archived. It can no longer be changed, and there is no way to reopen it.
        </p>
      </section>
    )
  }

  async function handleArchive() {
    setBusy(true)
    setError('')

    const result = await archiveSpace(space.public_id)
    setBusy(false)

    if (result.outcome === 'ok') {
      setConfirming(false)
      onArchived(result.data)
      return
    }

    setError(messageFor(result))
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4" data-testid="archive-panel">
      <h2 className="text-sm font-semibold text-slate-900">Archive</h2>
      <p className="mt-1 text-xs text-slate-500">
        Archiving closes the Space for good. This cannot be undone.
      </p>

      {confirming ? (
        <div className="mt-3" data-testid="archive-confirm">
          <p className="text-sm text-slate-900">
            Archive “{space.name}”? Members will no longer be able to change anything in it.
          </p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              className="rounded bg-red-700 px-3 py-1 text-sm text-white disabled:opacity-50"
              data-testid="archive-confirm-yes"
              disabled={busy}
              onClick={() => void handleArchive()}
            >
              {busy ? 'Archiving…' : 'Yes, archive it'}
            </button>
            <button
              type="button"
              className="rounded border border-slate-300 px-3 py-1 text-sm"
              data-testid="archive-cancel"
              disabled={busy}
              onClick={() => {
                setConfirming(false)
                setError('')
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          className="mt-3 rounded border border-red-300 px-3 py-1 text-sm text-red-700"
          data-testid="archive-start"
          onClick={() => setConfirming(true)}
        >
          Archive this Space
        </button>
      )}

      {error ? (
        <p className="mt-2 text-sm text-red-700" data-testid="archive-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  )
}
