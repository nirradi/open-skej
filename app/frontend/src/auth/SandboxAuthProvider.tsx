import { useEffect, type ReactNode } from 'react'

import { setAccessTokenProvider } from '../api'
import { getSandboxAccessToken } from './sandboxToken'

/**
 * The sandbox counterpart of `AuthProvider`'s `Auth0Provider` branch.
 *
 * Installs `getSandboxAccessToken` (`./sandboxToken`) as the api client's
 * token source and otherwise gets out of the way — it renders no login screen
 * and gates nothing, because sandbox mode exists to make a booking session
 * *deterministic*, not to reproduce Auth0's UI. `AuthProvider` selects this
 * component instead of `Auth0Provider` when `readSandboxConfig()` is true; it
 * is never rendered alongside it.
 *
 * Installation happens during render, not in an effect, for the same reason
 * `AccessTokenBridge` does it during render: render runs strictly
 * parent-first, so the provider is in place before any descendant's first
 * fetch. See that component's docstring for the full argument. The effect
 * below exists only to uninstall on unmount and to reinstall after
 * StrictMode's development-only mount/unmount/remount cycle.
 */
export function SandboxAuthProvider({ children }: { children: ReactNode }) {
  setAccessTokenProvider(getSandboxAccessToken)

  useEffect(() => {
    setAccessTokenProvider(getSandboxAccessToken)
    return () => setAccessTokenProvider(null)
  }, [])

  return <>{children}</>
}
