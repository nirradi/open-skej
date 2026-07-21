import type { ReactNode } from 'react'
import { Auth0Provider, type AppState } from '@auth0/auth0-react'

import { AccessTokenBridge } from './AccessTokenBridge'
import { MissingConfigNotice } from './MissingConfigNotice'
import { readAuth0Config } from './config'

/**
 * Restores the URL the user was on before they were sent to Auth0.
 *
 * Auth0 returns to `redirect_uri` with `?code=&state=` appended, which must not
 * survive into the address bar: it is noise, it breaks a refresh (the code is
 * single-use), and it is the sort of thing that ends up pasted into a bug
 * report. `replaceState` rather than `pushState` so the back button does not
 * walk into a spent authorization code.
 *
 * `history` directly rather than the router's navigate, because this runs
 * outside `<BrowserRouter>` — the provider wraps the router, not the reverse,
 * since a route may need to know whether anyone is signed in.
 */
function onRedirectCallback(appState?: AppState) {
  window.history.replaceState({}, document.title, appState?.returnTo ?? window.location.pathname)
}

/**
 * Wraps the app in Auth0, or in an explanation of why it could not be.
 *
 * Lives above the router so route components can read auth state, and above
 * `AccessTokenBridge` so the api client has a token before anything fetches.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const result = readAuth0Config()

  if (result.status === 'missing') {
    return <MissingConfigNotice missing={result.missing} />
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
      <AccessTokenBridge>{children}</AccessTokenBridge>
    </Auth0Provider>
  )
}
