// @vitest-environment jsdom
/**
 * Tests for the Auth0 adapter onto the mode-agnostic `Session` contract.
 *
 * `@auth0/auth0-react` is mocked wholesale — nothing here touches the live
 * tenant or opens a redirect. `AccessTokenBridge`'s own behaviour (installing
 * the api client's token source) is covered by its own test file; this one
 * is about the three-state status mapping and what `login`/`logout` pass to
 * the SDK.
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuth0 } from '@auth0/auth0-react'

import { Auth0SessionProvider } from './Auth0SessionProvider'
import { useSession } from './session'

vi.mock('@auth0/auth0-react', () => ({ useAuth0: vi.fn() }))

const loginWithRedirect = vi.fn()
const logout = vi.fn()
const getAccessTokenSilently = vi.fn(async () => 'a-jwt')

function auth0State(state: { isLoading?: boolean; isAuthenticated?: boolean }) {
  vi.mocked(useAuth0).mockReturnValue({
    isLoading: false,
    isAuthenticated: false,
    loginWithRedirect,
    logout,
    getAccessTokenSilently,
    ...state,
  } as unknown as ReturnType<typeof useAuth0>)
}

beforeEach(() => {
  auth0State({})
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

/** Surfaces `useSession()` as text so a test can assert on it without a DOM query per field. */
function SessionProbe() {
  const { status } = useSession()
  return <p data-testid="session-status">{status}</p>
}

function renderProvider() {
  return render(
    <Auth0SessionProvider>
      <SessionProbe />
    </Auth0SessionProvider>,
  )
}

describe('Auth0SessionProvider status mapping', () => {
  it('reports loading while the SDK is still initialising', () => {
    auth0State({ isLoading: true, isAuthenticated: false })
    renderProvider()
    expect(screen.getByTestId('session-status').textContent).toBe('loading')
  })

  it('reports unauthenticated once the SDK has settled with no session', () => {
    auth0State({ isLoading: false, isAuthenticated: false })
    renderProvider()
    expect(screen.getByTestId('session-status').textContent).toBe('unauthenticated')
  })

  it('reports authenticated when the SDK holds a session', () => {
    auth0State({ isLoading: false, isAuthenticated: true })
    renderProvider()
    expect(screen.getByTestId('session-status').textContent).toBe('authenticated')
  })
})

describe('Auth0SessionProvider login/logout', () => {
  function LoginButton() {
    const { login } = useSession()
    return (
      <button
        type="button"
        data-testid="do-login"
        onClick={() => login({ returnTo: '/s/abc123', connection: 'google-oauth2' })}
      >
        go
      </button>
    )
  }

  function LogoutButtonProbe() {
    const { logout } = useSession()
    return (
      <button type="button" data-testid="do-logout" onClick={() => logout()}>
        go
      </button>
    )
  }

  it('threads returnTo and connection through to loginWithRedirect', () => {
    render(
      <Auth0SessionProvider>
        <LoginButton />
      </Auth0SessionProvider>,
    )

    screen.getByTestId('do-login').click()

    expect(loginWithRedirect).toHaveBeenCalledWith({
      appState: { returnTo: '/s/abc123' },
      authorizationParams: { connection: 'google-oauth2' },
    })
  })

  it('logs out back to this origin', () => {
    render(
      <Auth0SessionProvider>
        <LogoutButtonProbe />
      </Auth0SessionProvider>,
    )

    screen.getByTestId('do-logout').click()

    expect(logout).toHaveBeenCalledWith({ logoutParams: { returnTo: window.location.origin } })
  })
})
