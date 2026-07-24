import { createContext, use } from 'react'

/**
 * Which of the three ways this build can establish a session, resolved once
 * by `AuthProvider` and threaded down so a route does not have to re-derive
 * it from the Auth0 env and the sandbox switch separately.
 *
 * A discriminated union rather than a plain enum because `unconfigured` is
 * the only case with anything else to say: `MissingConfigNotice` needs the
 * names of the missing `VITE_AUTH0_*` variables, and carrying them here means
 * a consumer reads one value instead of this context plus a second one just
 * for that list.
 *
 * `sandbox` is its own case, not folded into `auth0` or treated as
 * `unconfigured` — it is exactly the state that used to read as
 * unconfigured and take down `/account`, `/admin` and (as of this task) `/`
 * itself, since sandbox mode has no `VITE_AUTH0_*` variables set by design.
 * A route gating on this value distinguishes "no way to sign in at all" from
 * "sandbox is how signing in works here" without having to know sandbox mode
 * exists.
 */
export type AuthMode =
  | { kind: 'auth0' }
  | { kind: 'sandbox' }
  | { kind: 'unconfigured'; missing: string[] }

/**
 * Defaults to `unconfigured` with an empty list: a component reading this
 * outside any `AuthProvider` genuinely has no configuration, and defaulting
 * to `auth0` or `sandbox` would send it on to call a hook or read
 * `localStorage` for a mode nothing actually installed.
 */
export const AuthModeContext = createContext<AuthMode>({ kind: 'unconfigured', missing: [] })

/** Reads which auth mode the surrounding tree was built with. */
export function useAuthMode(): AuthMode {
  return use(AuthModeContext)
}
