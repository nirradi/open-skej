import { useEffect, useState } from 'react'
import { useAuth0 } from '@auth0/auth0-react'

import { getCurrentUser, type CurrentUser } from '../api'
import { LogoutButton } from './LoginControls'

/**
 * What the `GET /me` probe resolved to. `null` while it is still in flight.
 *
 * A separate shape from `GetCurrentUserResult` because the screen only has two
 * renderings — the row, or a message — so collapsing five outcomes into one
 * `message` here keeps the JSX from growing a branch per transport failure.
 */
type Probe = { kind: 'user'; user: CurrentUser } | { kind: 'error'; message: string } | null

/**
 * The signed-in user's own page.
 *
 * Small on purpose — task 2.9 owns the real dashboard. It earns its place by
 * being the first screen that exercises the whole chain end to end: the SDK
 * mints an access token, `AccessTokenBridge` hands it to the api client, the
 * client attaches it, and the backend accepts it and answers as a real user
 * row. Everything before this point can be green while the audience is wrong
 * and nobody would know.
 *
 * It is also where a just-in-time upsert and any pending invitation claim
 * actually happen, since both are side effects of `get_current_user` running.
 */
export function AccountPage() {
  const { user } = useAuth0()
  const [probe, setProbe] = useState<Probe>(null)

  useEffect(() => {
    let cancelled = false

    void getCurrentUser().then((result) => {
      if (cancelled) return
      setProbe(
        result.outcome === 'ok'
          ? { kind: 'user', user: result.data }
          : // Every non-ok outcome carries copy meant for a person; `invalid_request`
            // is the one that does not, because it describes a client bug.
            {
              kind: 'error',
              message:
                result.outcome === 'invalid_request'
                  ? 'Something went wrong on our end. Please try again.'
                  : result.message,
            },
      )
    })

    return () => {
      cancelled = true
    }
  }, [])

  return (
    <main className="min-h-screen bg-slate-50 p-8 text-slate-800">
      <div className="mx-auto max-w-md">
        <h1 className="text-2xl font-semibold text-slate-900">Your account</h1>

        <dl className="mt-6 space-y-2 text-sm" data-testid="account-identity">
          <div className="flex justify-between gap-4">
            <dt className="text-slate-500">Name</dt>
            <dd className="text-slate-900">{user?.name ?? '—'}</dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-slate-500">Email</dt>
            <dd className="text-slate-900">{user?.email ?? '—'}</dd>
          </div>
        </dl>

        <p className="mt-6 text-sm text-slate-600" data-testid="account-probe">
          {probe === null
            ? 'Checking your account with the server…'
            : probe.kind === 'user'
              ? `The server knows you as user #${probe.user.id}.`
              : probe.message}
        </p>

        <div className="mt-6">
          <LogoutButton />
        </div>
      </div>
    </main>
  )
}
