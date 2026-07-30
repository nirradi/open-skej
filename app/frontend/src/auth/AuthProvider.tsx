import type { ReactNode } from 'react'
import { Auth0Provider, type AppState } from '@auth0/auth0-react'

import { Auth0SessionProvider } from './Auth0SessionProvider'
import { SandboxAuthProvider } from './SandboxAuthProvider'
import { AuthModeContext } from './authConfigContext'
import { readAuth0Config } from './config'
import { setPostLoginRedirect } from './postLoginRedirectStore'
import { readSandboxConfig } from './sandboxConfig'

/**
 * Stashes the URL the user was on before they were sent to Auth0, for
 * `PostLoginRedirect` to apply once the router exists to apply it to.
 *
 * This runs outside `<BrowserRouter>` — the provider wraps the router, not
 * the reverse, since a route may need to know whether anyone is signed in —
 * and by the time the SDK calls it, `BrowserRouter` has already mounted at
 * the callback URL (`/`). `window.history.replaceState` used to be called
 * here directly, but that only rewrites the address bar; it tells React
 * Router nothing, so the URL read `/s/{public_id}` while `SpaceListPage`
 * stayed on screen. Publishing to the store and letting a router-aware
 * consumer navigate is what makes the rendered route agree with the address
 * bar — see `postLoginRedirectStore.ts` for why a store rather than a prop.
 */
function onRedirectCallback(appState?: AppState) {
  setPostLoginRedirect(appState?.returnTo ?? window.location.pathname)
}

/**
 * Wraps the app in Auth0 — or, when it cannot, gets out of the way.
 *
 * Lives above the router so route components can read auth state, and above
 * `AccessTokenBridge` so the api client has a token before anything fetches.
 *
 * ## An unconfigured tenant must not take the whole app down
 *
 * The tempting version of this returns `<MissingConfigNotice />` here and
 * renders nothing else. It is wrong, and the E2E suite caught it: the calendar
 * at `/` is unauthenticated and works perfectly well with no Auth0 tenant at
 * all, so replacing the entire app left twelve passing Stream 1 browser tests
 * staring at a configuration warning.
 *
 * So the failure is *reported*, not *enforced*, at this level. Children render
 * either way; the mode goes down as context, and `ProtectedRoute` and
 * `SpacePage` — the places that actually need a session — show the notice.
 * The blast radius ends up matching the dependency.
 *
 * A consequence worth naming: with config missing (and sandbox mode off)
 * there is no `Auth0Provider` below this and no `SessionContext` provider
 * either, so nothing below may call `useAuth0()` or assume `useSession()`
 * resolves to anything but its safe default. `ProtectedRoute` and `SpacePage`
 * handle this by checking `useAuthMode()` *before* delegating to a component
 * that reads the session.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  // `readSandboxConfig` throws if sandbox mode and any `VITE_AUTH0_*`
  // variable are both set — see that module. It is called unconditionally,
  // before `readAuth0Config`, so a misconfigured build fails here rather than
  // silently choosing Auth0 mode with the sandbox switch quietly ignored.
  const sandboxEnabled = readSandboxConfig()
  const result = readAuth0Config()

  if (sandboxEnabled) {
    // Mutual exclusivity above guarantees no `VITE_AUTH0_*` variable is set
    // here, so `result.status` is always `'missing'` in this branch — sandbox
    // mode installs its own session provider rather than standing in for
    // `Auth0Provider`, and the `sandbox` mode value is what tells
    // `ProtectedRoute`/`SpacePage` this is a configured, working way to sign
    // in rather than the genuine "nothing is configured" case.
    return (
      <AuthModeContext value={{ kind: 'sandbox' }}>
        <SandboxAuthProvider>{children}</SandboxAuthProvider>
      </AuthModeContext>
    )
  }

  if (result.status === 'missing') {
    return (
      <AuthModeContext value={{ kind: 'unconfigured', missing: result.missing }}>
        {children}
      </AuthModeContext>
    )
  }

  const { domain, clientId, audience } = result.config

  return (
    <Auth0Provider
      domain={domain}
      clientId={clientId}
      authorizationParams={{
        redirect_uri: window.location.origin,
        // Without this the SDK issues an *opaque* /userinfo token instead of a
        // JWT, and every backend call 401s behind a login that looked fine.
        // `readAuth0Config` requires it for exactly that reason.
        audience,
      }}
      onRedirectCallback={onRedirectCallback}
      // Silent renewal via a hidden iframe depends on third-party cookies, which
      // Safari blocks outright and Chrome is retiring — so the session would
      // die at the first token refresh for reasons invisible from our code.
      // Refresh tokens do not involve the iframe at all.
      useRefreshTokens
      // The refresh token has to outlive the tab for a reload to keep the user
      // signed in, which the default in-memory cache cannot do. This does put
      // the token where XSS could reach it; the mitigation is refresh-token
      // rotation (on by default for SPAs in the tenant provisioning script).
      // Worth revisiting alongside the first real deployment — see DEFERRED.md.
      cacheLocation="localstorage"
    >
      <AuthModeContext value={{ kind: 'auth0' }}>
        <Auth0SessionProvider>{children}</Auth0SessionProvider>
      </AuthModeContext>
    </Auth0Provider>
  )
}
