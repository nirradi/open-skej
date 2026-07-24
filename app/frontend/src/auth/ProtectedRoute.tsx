import type { ReactNode } from 'react'

import { LoginControls } from './LoginControls'
import { MissingConfigNotice } from './MissingConfigNotice'
import { useAuthMode } from './authConfigContext'
import { useSession } from './session'

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
 * ## Two checks, not one
 *
 * `useAuthMode()` answers "is there any way to sign in at all" — `auth0`,
 * `sandbox`, or genuinely `unconfigured` — and only the last of those shows
 * `MissingConfigNotice`. `sandbox` used to read as `unconfigured` too, back
 * when this only checked an Auth0-specific config status; that took `/`,
 * `/account` and `/admin` down for every sandbox build, including the whole
 * Playwright suite, which is exactly the bug this split exists to not
 * reintroduce. `useSession()` then answers the second, independent question —
 * "is *this visitor* signed in" — the same three-state shape regardless of
 * which mode is running underneath.
 *
 * ## Why it renders login rather than redirecting
 *
 * An automatic redirect (Auth0's `loginWithRedirect`, called through
 * `session.login()`) would send a user who followed a link straight off the
 * site before they had read a word of it, and there is nowhere to show *why*
 * they were bounced. Rendering the controls in place keeps the URL intact —
 * which is what lets `LoginControls` return the user to this exact route
 * afterwards.
 */
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const mode = useAuthMode()

  if (mode.kind === 'unconfigured') {
    return <MissingConfigNotice missing={mode.missing} />
  }

  return <SignedInGate>{children}</SignedInGate>
}

/** The actual gate. Only ever rendered once some session provider is mounted. */
function SignedInGate({ children }: { children: ReactNode }) {
  const { status } = useSession()

  if (status === 'loading') {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-50 p-8">
        <p className="text-sm text-slate-600" data-testid="auth-loading" role="status">
          Checking your session…
        </p>
      </main>
    )
  }

  if (status === 'unauthenticated') {
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
