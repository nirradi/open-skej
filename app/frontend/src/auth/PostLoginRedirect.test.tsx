// @vitest-environment jsdom
/**
 * The acceptance test for task 5.5: a deep link survives the real Auth0
 * round trip.
 *
 * `AuthProvider`'s `onRedirectCallback` used to call
 * `window.history.replaceState`, which rewrites the address bar and tells
 * React Router nothing. `BrowserRouter` lives inside `App`, below
 * `Auth0Provider`, and has already mounted at the callback URL (`/`) by the
 * time the SDK processes the callback — so the URL read `/account` while
 * `SpaceListPage` stayed on screen. Asserting the URL, as a naive version of
 * this test would, is exactly the assertion that passed on the old, broken
 * code: `replaceState` genuinely does rewrite `window.location`. The bug is
 * only visible in **what is rendered**, so that is what this test checks.
 *
 * `@auth0/auth0-react` is mocked wholesale, the same idiom
 * `AuthProvider.test.tsx` and `Auth0SessionProvider.test.tsx` already use.
 * `Auth0Provider` is stubbed to capture the `onRedirectCallback` prop it is
 * given and render its children directly — this is what lets the test
 * invoke the real callback `AuthProvider` wires up, rather than
 * reimplementing it. `useAuth0` reports an already-authenticated session,
 * the state the SDK is in by the time it actually calls the callback.
 */

import type { ReactNode } from 'react'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AppState } from '@auth0/auth0-react'

import App from '../App'
import * as api from '../api'
import { AuthProvider } from './AuthProvider'
import { readAuth0Config } from './config'
import { clearPostLoginRedirect } from './postLoginRedirectStore'
import { readSandboxConfig } from './sandboxConfig'

const { capturedCallback } = vi.hoisted(() => ({
  capturedCallback: { current: undefined as ((appState?: AppState) => void) | undefined },
}))

vi.mock('@auth0/auth0-react', () => ({
  Auth0Provider: ({
    children,
    onRedirectCallback,
  }: {
    children: ReactNode
    onRedirectCallback?: (appState?: AppState) => void
  }) => {
    capturedCallback.current = onRedirectCallback
    return children
  },
  useAuth0: () => ({
    isLoading: false,
    isAuthenticated: true,
    loginWithRedirect: vi.fn(),
    logout: vi.fn(),
    getAccessTokenSilently: vi.fn(async () => 'a-jwt'),
  }),
}))

vi.mock('./config', () => ({ readAuth0Config: vi.fn() }))
vi.mock('./sandboxConfig', () => ({ readSandboxConfig: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  clearPostLoginRedirect()
  capturedCallback.current = undefined
})

function renderSignedInApp() {
  vi.mocked(readSandboxConfig).mockReturnValue(false)
  vi.mocked(readAuth0Config).mockReturnValue({
    status: 'ok',
    config: {
      domain: 'skej.auth0.com',
      clientId: 'client-id',
      audience: 'https://api.open-skej.dev',
    },
  })
  vi.spyOn(api, 'listSpaces').mockResolvedValue({ outcome: 'ok', data: [] })
  vi.spyOn(api, 'getCurrentUser').mockResolvedValue({
    outcome: 'ok',
    data: { id: 1, email: 'a@example.com', name: 'A', last_login_at: null },
  })

  window.history.pushState({}, '', '/')

  return render(
    <AuthProvider>
      <App />
    </AuthProvider>,
  )
}

describe('the deep link survives the real Auth0 round trip', () => {
  it('renders the returnTo route, not the post-login default', async () => {
    renderSignedInApp()

    // The router mounts at `/` first, same as the real bug: the callback has
    // not fired yet, so the Space list is what is on screen to start.
    await screen.findByTestId('space-list-loading')

    act(() => {
      capturedCallback.current?.({ returnTo: '/account' })
    })

    // The rendered route, not `window.location` — see the file docstring
    // for why the URL alone is not what this test may assert on.
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Your account' })).toBeTruthy(),
    )
    expect(screen.queryByTestId('space-list-loading')).toBeNull()
    expect(screen.queryByTestId('space-list-empty')).toBeNull()
    expect(window.location.pathname).toBe('/account')
  })
})
