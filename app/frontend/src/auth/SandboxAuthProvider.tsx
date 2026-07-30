import { useCallback, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react'

import {
  accessTokenProviderEpoch,
  clearAccessTokenProviderIf,
  clearSessionLost,
  getSessionLostSnapshot,
  setAccessTokenProvider,
  subscribeSessionLost,
  type AccessTokenProvider,
} from '../api'
import { getSandboxAccessToken } from './sandboxToken'
import { SessionContext, type Session } from './session'

/**
 * The `localStorage` key that models sandbox mode's signed-in/signed-out
 * session, distinct from `sandboxToken.ts`'s `SANDBOX_SUB_STORAGE_KEY`.
 *
 * The two keys answer different questions on purpose. `SANDBOX_SUB_STORAGE_KEY`
 * is "which seeded identity would a mint use" and is read fresh on every token
 * request — `signInAsSandbox` in `app/e2e/tests/fixtures.ts` relies on that to
 * pin an identity before a navigation with no re-render involved. This key is
 * "has that identity been committed" — the fact `useSession().status` reports.
 * Folding the two together would make *selecting* an identity indistinguishable
 * from *signing in as* it, which is exactly the distinction the deep-link
 * E2E spec needs: a visitor can arrive with an identity already chosen and
 * still see the sign-in card, then commit it by clicking through — the same
 * shape a real Auth0 visitor has, choosing a Google account without yet
 * having pressed the button.
 */
export const SANDBOX_SIGNED_IN_STORAGE_KEY = 'skej.sandbox.signedIn'

function readSignedIn(): boolean {
  return window.localStorage.getItem(SANDBOX_SIGNED_IN_STORAGE_KEY) === 'true'
}

/**
 * Mirrors `getAccessTokenSilently`'s rejection when no session exists: the
 * real SDK rejects with `login_required` rather than the api client sending
 * an anonymous request, and `client.ts#authorizationHeader` turns that
 * rejection into the `unauthenticated` outcome. Sandbox mode reproduces that
 * so a signed-out visitor gets the same `unauthenticated` result a real
 * expired-session visitor would, rather than silently authenticating as
 * whichever identity happens to be selected in `localStorage`.
 */
async function rejectSignedOut(): Promise<string> {
  throw new Error('Sandbox session is signed out')
}

/**
 * The sandbox counterpart of `Auth0SessionProvider`.
 *
 * Installs `getSandboxAccessToken` as the api client's token source only once
 * `login()` has committed an identity; before that, and after `logout()`, it
 * installs `rejectSignedOut` instead. `AuthProvider` selects this component in
 * place of `Auth0Provider`/`Auth0SessionProvider` when `readSandboxConfig()`
 * is true; it is never rendered alongside either.
 *
 * Installation happens during render, not only in an effect, for the same
 * reason `AccessTokenBridge` does it during render: render runs strictly
 * parent-first, so the provider is in place before any descendant's first
 * fetch. The effect below exists only to uninstall on unmount and to
 * reinstall after StrictMode's development-only mount/unmount/remount cycle.
 *
 * ## `sessionLost` overrides `signedIn`, not the other way round
 *
 * `signedIn` only ever reflects whether `login()`/`logout()` was called — it
 * has no way to learn that `getSandboxAccessToken` started rejecting mid-
 * session, which is exactly the sandbox-mode shape of the bug task 5.4
 * fixes: a lapsed sandbox token would otherwise keep `useSession()`
 * answering `authenticated` forever. `client.ts`'s session-lost store (read
 * with `useSyncExternalStore`, not a `useEffect` + `setState` — the latter is
 * both the wrong primitive for a value that changes outside React and
 * forbidden by this repo's eslint config) is where that failure is actually
 * discovered, so it wins over `signedIn` whenever both have an opinion.
 * `signedIn` itself, and the token provider keyed on it below, are
 * deliberately left alone: `login()` is the only thing that clears
 * `sessionLost`, so nothing here can quietly re-arm silent auth on its own.
 */
export function SandboxAuthProvider({ children }: { children: ReactNode }) {
  const [signedIn, setSignedIn] = useState(readSignedIn)
  const sessionLost = useSyncExternalStore(subscribeSessionLost, getSessionLostSnapshot)

  // Ignores its argument entirely: `connection` and `screenHint` steer a
  // hosted login screen sandbox mode does not have, and there is no identity
  // to pass — that was already chosen via `localStorage` before this runs.
  const login = useCallback(() => {
    // The only place this clears — see the `sessionLost` note above.
    clearSessionLost()
    window.localStorage.setItem(SANDBOX_SIGNED_IN_STORAGE_KEY, 'true')
    setSignedIn(true)
  }, [])

  const logout = useCallback(() => {
    window.localStorage.removeItem(SANDBOX_SIGNED_IN_STORAGE_KEY)
    setSignedIn(false)
  }, [])

  const session: Session = useMemo(
    () => ({
      status: sessionLost ? 'unauthenticated' : signedIn ? 'authenticated' : 'unauthenticated',
      login,
      logout,
    }),
    [sessionLost, signedIn, login, logout],
  )

  const provider: AccessTokenProvider = signedIn ? getSandboxAccessToken : rejectSignedOut
  setAccessTokenProvider(provider)

  useEffect(() => {
    setAccessTokenProvider(provider)
    // Deferred and conditional — the same window `AccessTokenBridge` closes,
    // and the one the E2E suite actually runs into: effects run child-first,
    // so nulling unconditionally here sends every child's remount refetch out
    // with no `Authorization` header. See `clearAccessTokenProviderIf`.
    const epoch = accessTokenProviderEpoch()
    return () => queueMicrotask(() => clearAccessTokenProviderIf(epoch))
  }, [provider])

  return <SessionContext value={session}>{children}</SessionContext>
}
