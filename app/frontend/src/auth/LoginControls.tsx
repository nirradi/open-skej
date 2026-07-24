import { useSession } from './session'

/**
 * The Auth0 identifier for the tenant's Google social connection.
 *
 * Passing it as `connection` skips the Universal Login account picker and sends
 * the user straight to Google. Omitting the parameter entirely (the "Continue
 * with email" path below) shows Auth0's own screen, which offers the database
 * connection *and* Google — so the two buttons differ only in how many clicks
 * the Google path takes, and neither can lock a user out of a method.
 *
 * Meaningless to the sandbox session — `SandboxAuthProvider.login()` ignores
 * it — since there is no hosted screen to steer and both buttons commit the
 * same already-selected identity.
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
 * `returnTo` is passed to `session.login()`, which threads it through however
 * the running mode gets back here — Auth0's `appState`, read back out by
 * `AuthProvider`'s `onRedirectCallback`; sandbox's synchronous commit, which
 * never leaves the page in the first place. Either way, a user bounced to
 * login from a deep link — a Space's share link, which is the whole
 * distribution model — lands back on it rather than on the Space list.
 */
export function LoginControls({ returnTo }: { returnTo?: string }) {
  const { login } = useSession()
  const effectiveReturnTo = returnTo ?? `${window.location.pathname}${window.location.search}`

  return (
    <div className="flex flex-col gap-2" data-testid="login-controls">
      <button
        type="button"
        className={BUTTON_CLASS}
        data-testid="login-google"
        onClick={() => login({ returnTo: effectiveReturnTo, connection: GOOGLE_CONNECTION })}
      >
        Continue with Google
      </button>
      <button
        type="button"
        className={BUTTON_CLASS}
        data-testid="login-email"
        onClick={() => login({ returnTo: effectiveReturnTo })}
      >
        Continue with email
      </button>
    </div>
  )
}

/**
 * Signs the user out of the current session.
 *
 * The api client's token provider is deliberately left installed by both
 * `Session` implementations rather than uninstalled here: each already turns
 * "no session" into a rejecting provider on its own (a cleared Auth0 cache, a
 * cleared sandbox flag), and the api client already turns a rejection into
 * the `unauthenticated` outcome — the same branch a 401 takes. One path for
 * "not signed in" rather than two.
 */
export function LogoutButton() {
  const { logout } = useSession()

  return (
    <button type="button" className={BUTTON_CLASS} data-testid="logout" onClick={() => logout()}>
      Sign out
    </button>
  )
}
