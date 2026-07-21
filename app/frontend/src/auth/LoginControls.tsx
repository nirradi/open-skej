import { useAuth0 } from '@auth0/auth0-react'

/**
 * The Auth0 identifier for the tenant's Google social connection.
 *
 * Passing it as `connection` skips the Universal Login account picker and sends
 * the user straight to Google. Omitting the parameter entirely (the "Continue
 * with email" path below) shows Auth0's own screen, which offers the database
 * connection *and* Google — so the two buttons differ only in how many clicks
 * the Google path takes, and neither can lock a user out of a method.
 *
 * Enabled on the SPA client by `scripts/auth0_provision.py`, not by hand in the
 * dashboard.
 */
const GOOGLE_CONNECTION = 'google-oauth2'

const BUTTON_CLASS =
  'w-full rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium ' +
  'text-slate-800 transition hover:bg-slate-50 focus:outline-none focus:ring-2 ' +
  'focus:ring-slate-400 focus:ring-offset-1'

/**
 * The two ways in, plus the return path.
 *
 * `returnTo` is threaded through `appState` so that a user bounced to login from
 * a deep link — a Space's share link, which is the whole distribution model —
 * lands back on it rather than on the calendar. `AuthProvider`'s
 * `onRedirectCallback` is what reads it back out.
 */
export function LoginControls({ returnTo }: { returnTo?: string }) {
  const { loginWithRedirect } = useAuth0()
  const appState = { returnTo: returnTo ?? `${window.location.pathname}${window.location.search}` }

  return (
    <div className="flex flex-col gap-2" data-testid="login-controls">
      <button
        type="button"
        className={BUTTON_CLASS}
        data-testid="login-google"
        onClick={() =>
          loginWithRedirect({
            appState,
            authorizationParams: { connection: GOOGLE_CONNECTION },
          })
        }
      >
        Continue with Google
      </button>
      <button
        type="button"
        className={BUTTON_CLASS}
        data-testid="login-email"
        onClick={() => loginWithRedirect({ appState })}
      >
        Continue with email
      </button>
    </div>
  )
}

/**
 * Signs the user out of both Auth0 and this app.
 *
 * `logoutParams.returnTo` must be an allow-listed logout URL on the SPA client
 * or Auth0 refuses the redirect and strands the user on its own error page —
 * `scripts/auth0_provision.py` registers `http://localhost:5173` for this.
 *
 * The api client's token provider is deliberately left installed: the SDK has
 * already cleared its cache, so the provider now rejects, and the client turns
 * that into the `unauthenticated` outcome — the same branch a 401 takes. One
 * path for "not signed in" rather than two.
 */
export function LogoutButton() {
  const { logout } = useAuth0()

  return (
    <button
      type="button"
      className={BUTTON_CLASS}
      data-testid="logout"
      onClick={() => logout({ logoutParams: { returnTo: window.location.origin } })}
    >
      Sign out
    </button>
  )
}
