// @vitest-environment jsdom
/**
 * Tests for the route guard and the login controls.
 *
 * `@auth0/auth0-react` is mocked wholesale. Nothing here touches the live
 * tenant, opens a redirect, or needs a network — the SDK's job is to produce
 * `{ isLoading, isAuthenticated }` and a `loginWithRedirect`, and what this
 * component does with those three is the entire subject.
 *
 * The state worth the most attention is `isLoading`. It is the one a two-branch
 * implementation drops, and dropping it is not a cosmetic bug: the SDK reports
 * `isAuthenticated: false` while it checks for an existing session, so a guard
 * that skipped it would flash the login screen at every already-signed-in user
 * on every page load.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuth0 } from '@auth0/auth0-react'

import { AuthConfigContext } from './authConfigContext'
import type { Auth0ConfigResult } from './config'
import { LoginControls, LogoutButton } from './LoginControls'
import { ProtectedRoute } from './ProtectedRoute'

vi.mock('@auth0/auth0-react', () => ({ useAuth0: vi.fn() }))

const loginWithRedirect = vi.fn()
const logout = vi.fn()

/** Puts the mocked SDK into one of its three states. */
function auth0State(state: { isLoading?: boolean; isAuthenticated?: boolean }) {
  vi.mocked(useAuth0).mockReturnValue({
    isLoading: false,
    isAuthenticated: false,
    loginWithRedirect,
    logout,
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

/** A tenant that is configured, which is the precondition for the gate to run. */
const CONFIGURED: Auth0ConfigResult = {
  status: 'ok',
  config: { domain: 'd.auth0.com', clientId: 'c', audience: 'https://api.open-skej.dev' },
}

function guarded(config: Auth0ConfigResult = CONFIGURED) {
  return (
    <AuthConfigContext value={config}>
      <ProtectedRoute>
        <p data-testid="secret">Members only</p>
      </ProtectedRoute>
    </AuthConfigContext>
  )
}

function renderGuarded(config: Auth0ConfigResult = CONFIGURED) {
  return render(guarded(config))
}

describe('ProtectedRoute', () => {
  it('renders its children when the user is authenticated', () => {
    auth0State({ isAuthenticated: true })

    renderGuarded()

    expect(screen.getByTestId('secret')).toBeTruthy()
    expect(screen.queryByTestId('auth-required')).toBeNull()
  })

  it('renders the login controls when the user is not authenticated', () => {
    auth0State({ isAuthenticated: false })

    renderGuarded()

    expect(screen.getByTestId('auth-required')).toBeTruthy()
    expect(screen.getByTestId('login-controls')).toBeTruthy()
    // The protected content must not be in the DOM at all. Hiding it with CSS
    // while it sat in the markup would leak whatever it rendered.
    expect(screen.queryByTestId('secret')).toBeNull()
  })

  it('shows a loading state while the SDK is still initialising', () => {
    auth0State({ isLoading: true, isAuthenticated: false })

    renderGuarded()

    expect(screen.getByTestId('auth-loading')).toBeTruthy()
    // Neither the content nor the login prompt: the answer is not known yet,
    // and guessing either way is visibly wrong half the time.
    expect(screen.queryByTestId('secret')).toBeNull()
    expect(screen.queryByTestId('auth-required')).toBeNull()
  })

  it('does not redirect to Auth0 on its own', () => {
    // An automatic `loginWithRedirect` would throw an unauthenticated visitor
    // off the site before they had read anything, and — combined with the
    // `isLoading` window above — is the classic way to build a redirect loop.
    auth0State({ isAuthenticated: false })

    renderGuarded()

    expect(loginWithRedirect).not.toHaveBeenCalled()
  })

  it('keeps loading distinct from signed out across a state change', () => {
    // The regression that matters: an already-signed-in user must never see the
    // login prompt on the way to their page.
    auth0State({ isLoading: true, isAuthenticated: false })
    const { rerender } = renderGuarded()
    expect(screen.getByTestId('auth-loading')).toBeTruthy()

    auth0State({ isLoading: false, isAuthenticated: true })
    rerender(guarded())

    expect(screen.getByTestId('secret')).toBeTruthy()
    expect(screen.queryByTestId('auth-required')).toBeNull()
  })

  it('explains an unconfigured tenant instead of asking for a login that cannot work', () => {
    // With no `VITE_AUTH0_*` there is no `Auth0Provider` in the tree, so this
    // branch also has to render without ever calling `useAuth0` — which is why
    // the check lives in a component above the one holding the hook.
    auth0State({ isAuthenticated: false })

    renderGuarded({ status: 'missing', missing: ['VITE_AUTH0_DOMAIN'] })

    expect(screen.getByTestId('auth-config-missing')).toBeTruthy()
    expect(screen.getByText('VITE_AUTH0_DOMAIN')).toBeTruthy()
    // A login button here would be a dead end: there is no tenant to send
    // anyone to.
    expect(screen.queryByTestId('login-controls')).toBeNull()
    expect(screen.queryByTestId('secret')).toBeNull()
  })
})

describe('LoginControls', () => {
  it('offers both Google and email/password', () => {
    render(<LoginControls />)

    expect(screen.getByTestId('login-google')).toBeTruthy()
    expect(screen.getByTestId('login-email')).toBeTruthy()
  })

  it('sends the Google button straight to the google-oauth2 connection', () => {
    render(<LoginControls />)

    fireEvent.click(screen.getByTestId('login-google'))

    expect(loginWithRedirect).toHaveBeenCalledTimes(1)
    // The exact string the provisioning script enables on the SPA client. A
    // typo here degrades silently into the generic login screen rather than
    // erroring, so it is asserted rather than eyeballed.
    expect(loginWithRedirect.mock.calls[0][0].authorizationParams.connection).toBe('google-oauth2')
  })

  it('sends the email button to Universal Login with no connection pinned', () => {
    render(<LoginControls />)

    fireEvent.click(screen.getByTestId('login-email'))

    const args = loginWithRedirect.mock.calls[0][0]
    // No `connection` means Auth0's own screen, which offers the database
    // connection *and* Google — so neither button can strand a user who picked
    // the other method last time.
    expect(args.authorizationParams?.connection).toBeUndefined()
  })

  it('remembers where to come back to', () => {
    // A Space share link is the entire distribution model: a user who follows
    // one and is asked to sign in has to land back on that link afterwards, not
    // on the calendar.
    render(<LoginControls returnTo="/s/abc123" />)

    fireEvent.click(screen.getByTestId('login-email'))

    expect(loginWithRedirect.mock.calls[0][0].appState.returnTo).toBe('/s/abc123')
  })

  it('defaults returnTo to the current location', () => {
    render(<LoginControls />)

    fireEvent.click(screen.getByTestId('login-google'))

    expect(loginWithRedirect.mock.calls[0][0].appState.returnTo).toBe(window.location.pathname)
  })
})

describe('LogoutButton', () => {
  it('logs out back to this origin', () => {
    render(<LogoutButton />)

    fireEvent.click(screen.getByTestId('logout'))

    // Auth0 refuses a `returnTo` that is not in the client's allowed logout
    // URLs and strands the user on its own error page, so this value has to
    // match what `auth0_provision.py` registers.
    expect(logout).toHaveBeenCalledWith({ logoutParams: { returnTo: window.location.origin } })
  })
})
