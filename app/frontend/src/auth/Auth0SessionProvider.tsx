import { useCallback, useMemo, type ReactNode } from 'react'
import { useAuth0 } from '@auth0/auth0-react'

import { AccessTokenBridge } from './AccessTokenBridge'
import { SessionContext, type Session, type SessionLoginOptions } from './session'

/**
 * Adapts the Auth0 SDK onto the mode-agnostic `Session` contract.
 *
 * Lives inside `Auth0Provider` (it calls `useAuth0()`) and wraps
 * `AccessTokenBridge`, which still owns installing the api client's token
 * source — that seam is unrelated to *which screen renders*, only to what
 * every fetch carries, so it stays a separate component with its own test
 * file rather than being folded in here.
 *
 * `isLoading` becomes its own status rather than being squashed into
 * `unauthenticated`: the SDK reports `isAuthenticated: false` on every page
 * load while it checks for an existing session, and a consumer that treated
 * that as "signed out" would flash a login screen at an already-signed-in
 * user. `ProtectedRoute` and `SpacePage` used to each guard against this
 * themselves; centralising it here means neither has to remember to.
 */
export function Auth0SessionProvider({ children }: { children: ReactNode }) {
  const { isLoading, isAuthenticated, loginWithRedirect, logout: auth0Logout } = useAuth0()

  const login = useCallback(
    (options?: SessionLoginOptions) => {
      const returnTo = options?.returnTo ?? `${window.location.pathname}${window.location.search}`
      void loginWithRedirect({
        appState: { returnTo },
        authorizationParams: {
          ...(options?.connection ? { connection: options.connection } : {}),
          ...(options?.screenHint ? { screen_hint: options.screenHint } : {}),
        },
      })
    },
    [loginWithRedirect],
  )

  // `logoutParams.returnTo` must be an allow-listed logout URL on the SPA
  // client — `scripts/auth0_provision.py` registers `http://localhost:5173`
  // for exactly this call — or Auth0 refuses the redirect and strands the
  // user on its own error page.
  const logout = useCallback(() => {
    auth0Logout({ logoutParams: { returnTo: window.location.origin } })
  }, [auth0Logout])

  const session: Session = useMemo(
    () => ({
      status: isLoading ? 'loading' : isAuthenticated ? 'authenticated' : 'unauthenticated',
      login,
      logout,
    }),
    [isLoading, isAuthenticated, login, logout],
  )

  return (
    <SessionContext value={session}>
      <AccessTokenBridge>{children}</AccessTokenBridge>
    </SessionContext>
  )
}
