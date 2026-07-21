import type { ReactNode } from 'react'
import { useAuth0 } from '@auth0/auth0-react'

import { LoginControls } from './LoginControls'
import { MissingConfigNotice } from './MissingConfigNotice'
import { useAuthConfig } from './authConfigContext'

/**
 * Gates its children on being signed in.
 *
 * ## This is not a security boundary
 *
 * Every route it wraps is also enforced server-side — `get_current_user` and
 * `require_space_role` decide what a caller may see, and this component cannot
 * be the thing that stops them, since anyone can edit the bundle they were
 * served. What it *is* for is not rendering a members-only screen that would
 * immediately fill with 401s, which is a usability job, not a safety one.
 *
 * ## Three states, not two
 *
 * `isLoading` is the state that gets forgotten, and skipping it produces a
 * specific, reproducible bug: on every page load the SDK starts out
 * unauthenticated while it checks for an existing session, so treating
 * `!isAuthenticated` as "signed out" flashes the login screen at an already
 * signed-in user for a few hundred milliseconds before swapping it for the real
 * page. Worse, if the unauthenticated branch redirected to Auth0 automatically,
 * that flash would become a redirect loop.
 *
 * ## Why it renders login rather than redirecting
 *
 * An automatic `loginWithRedirect` would send a user who followed a link
 * straight off the site before they had read a word of it, and there is nowhere
 * to show *why* they were bounced. Rendering the controls in place keeps the URL
 * intact — which is what lets `LoginControls` return the user to this exact
 * route afterwards.
 *
 * ## Why the config check is a separate component
 *
 * With Auth0 unconfigured there is no `Auth0Provider` in the tree at all — see
 * `AuthProvider`, which deliberately keeps rendering the app rather than taking
 * the unauthenticated calendar down with it. `useAuth0()` therefore must not be
 * called in that state, and a hook cannot be called conditionally. Splitting
 * the check into this outer component and the hook into `SignedInGate` below
 * keeps both rules satisfied without a conditional hook.
 */
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const config = useAuthConfig()

  if (config.status === 'missing') {
    return <MissingConfigNotice missing={config.missing} />
  }

  return <SignedInGate>{children}</SignedInGate>
}

/** The actual gate. Only ever rendered inside a configured `Auth0Provider`. */
function SignedInGate({ children }: { children: ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth0()

  if (isLoading) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-50 p-8">
        <p className="text-sm text-slate-600" data-testid="auth-loading" role="status">
          Checking your session…
        </p>
      </main>
    )
  }

  if (!isAuthenticated) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-50 p-8">
        <div
          className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
          data-testid="auth-required"
        >
          <h1 className="text-lg font-semibold text-slate-900">Sign in to continue</h1>
          <p className="mt-2 mb-4 text-sm text-slate-600">You need an account to see this page.</p>
          <LoginControls />
        </div>
      </main>
    )
  }

  return <>{children}</>
}
