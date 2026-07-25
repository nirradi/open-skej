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

  return (
    <main className={PAGE_CLASS}>
      <div className="mx-auto max-w-md">
        <h1 className="text-2xl font-semibold text-slate-900">Your Spaces</h1>

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
