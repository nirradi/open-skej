import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { listSpaces, type Space } from '../api'
import { messageFor } from '../ui/messages'

type Load = { kind: 'ok'; spaces: Space[] } | { kind: 'error'; message: string } | null

const PAGE_CLASS = 'min-h-screen bg-slate-50 p-8 text-slate-800'

/**
 * `/` for a signed-in visitor — the post-login destination `ProtectedRoute`
 * lands them on instead of a generic calendar.
 *
 * A member clicking through lands on `SpacePage`, which renders that Space —
 * its name, description, and a picker onto its Resources — rather than
 * bouncing back here; this list is where you start, not where every link
 * inside a Space returns you to.
 *
 * `listSpaces()` is memberships, not a directory — see its own docstring. A
 * Space this user has no relationship with is not filtered out of the
 * response, it is absent from the database's answer entirely.
 *
 * **`/admin` is linked from here once, not per row.** `Space.my_role` travels
 * on every entry, so this is the one screen every signed-in admin or owner is
 * guaranteed to reach — the fact this whole task exists to fix, since
 * previously nothing in the app pointed at the console at all. The console
 * itself opens on the first Space in `listSpaces` order regardless of which
 * row sent you there, so a link per row would promise something a click on it
 * would not deliver; one link, shown whenever any Space here makes the caller
 * at least an admin, is what the destination actually supports. As with every
 * role-gated control in this app, this is a convenience and never a security
 * boundary — `require_space_role` decides what the console actually lets you
 * do once you are on it.
 */
export function SpaceListPage() {
  const [load, setLoad] = useState<Load>(null)

  useEffect(() => {
    let cancelled = false

    void listSpaces().then((result) => {
      if (cancelled) return

      if (result.outcome === 'ok') {
        setLoad({ kind: 'ok', spaces: result.data })
      } else {
        setLoad({ kind: 'error', message: messageFor(result) })
      }
    })

    return () => {
      cancelled = true
    }
  }, [])

  if (load === null) {
    return (
      <main className={PAGE_CLASS}>
        <p className="text-sm text-slate-600" data-testid="space-list-loading" role="status">
          Loading your Spaces…
        </p>
      </main>
    )
  }

  if (load.kind === 'error') {
    return (
      <main className={PAGE_CLASS}>
        <p className="text-sm text-red-700" data-testid="space-list-error" role="alert">
          {load.message}
        </p>
      </main>
    )
  }

  const canManageASpace = load.spaces.some(
    (space) => space.my_role === 'admin' || space.my_role === 'owner',
  )

  return (
    <main className={PAGE_CLASS}>
      <div className="mx-auto max-w-md">
        <div className="flex items-center justify-between gap-4">
          <h1 className="text-2xl font-semibold text-slate-900">Your Spaces</h1>
          {canManageASpace && (
            <Link
              to="/admin"
              className="text-sm font-medium text-slate-600 hover:underline"
              data-testid="admin-link"
            >
              Manage a Space
            </Link>
          )}
        </div>

        {load.spaces.length === 0 ? (
          <p className="mt-4 text-sm text-slate-600" data-testid="space-list-empty">
            You&rsquo;re not a member of any Space yet. Ask whoever runs one for its link.
          </p>
        ) : (
          <ul className="mt-4 space-y-2" data-testid="space-list">
            {load.spaces.map((space) => (
              <li
                key={space.public_id}
                className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm"
              >
                <Link
                  to={`/s/${space.public_id}`}
                  className="font-medium text-slate-900 hover:underline"
                  data-testid={`space-list-item-${space.public_id}`}
                >
                  {space.name}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </main>
  )
}
