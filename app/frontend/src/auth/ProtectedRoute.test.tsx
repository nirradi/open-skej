// @vitest-environment jsdom
/**
 * Tests for the route guard and the login controls.
 *
 * Neither `ProtectedRoute` nor `LoginControls` imports `@auth0/auth0-react`
 * any more — both read the mode-agnostic `useAuthMode()`/`useSession()`
 * seam instead, so this file supplies fake context values rather than
 * mocking the SDK. That is the point of the abstraction: the same test
 * shape exercises the gate regardless of which mode would really be
 * installed underneath it.
 *
 * The state worth the most attention is still `loading`. It is the one a
 * two-branch implementation drops, and dropping it is not cosmetic: a real
 * session reports `unauthenticated` while it checks for an existing session,
 * so a guard that skipped it would flash the login screen at every
 * already-signed-in user on every page load.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AuthModeContext, type AuthMode } from './authConfigContext'
import { LoginControls, LogoutButton } from './LoginControls'
import { ProtectedRoute } from './ProtectedRoute'
import { SessionContext, type Session, type SessionStatus } from './session'

const login = vi.fn()
const logout = vi.fn()

function sessionState(status: SessionStatus): Session {
  return { status, login, logout }
}

/** A mode that is a working way to sign in, which is the precondition for the gate to run. */
const CONFIGURED: AuthMode = { kind: 'auth0' }

function guarded(mode: AuthMode = CONFIGURED, status: SessionStatus = 'unauthenticated') {
  return (
    <AuthModeContext value={mode}>
      <SessionContext value={sessionState(status)}>
        <ProtectedRoute>
          <p data-testid="secret">Members only</p>
        </ProtectedRoute>
      </SessionContext>
    </AuthModeContext>
  )
}

function renderGuarded(mode: AuthMode = CONFIGURED, status: SessionStatus = 'unauthenticated') {
  return render(guarded(mode, status))
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ProtectedRoute', () => {
  it('renders its children when the user is authenticated', () => {
    renderGuarded(CONFIGURED, 'authenticated')

    expect(screen.getByTestId('secret')).toBeTruthy()
    expect(screen.queryByTestId('auth-required')).toBeNull()
  })

  it('renders the login controls when the user is not authenticated', () => {
    renderGuarded(CONFIGURED, 'unauthenticated')

    expect(screen.getByTestId('auth-required')).toBeTruthy()
    expect(screen.getByTestId('login-controls')).toBeTruthy()
    // The protected content must not be in the DOM at all. Hiding it with CSS
    // while it sat in the markup would leak whatever it rendered.
    expect(screen.queryByTestId('secret')).toBeNull()
  })

  it('shows a loading state while the session is still resolving', () => {
    renderGuarded(CONFIGURED, 'loading')

    expect(screen.getByTestId('auth-loading')).toBeTruthy()
    // Neither the content nor the login prompt: the answer is not known yet,
    // and guessing either way is visibly wrong half the time.
    expect(screen.queryByTestId('secret')).toBeNull()
    expect(screen.queryByTestId('auth-required')).toBeNull()
  })

  it('does not call login on its own', () => {
    // An automatic login would throw an unauthenticated visitor off the site
    // before they had read anything, and — combined with the `loading` window
    // above — is the classic way to build a redirect loop.
    renderGuarded(CONFIGURED, 'unauthenticated')

    expect(login).not.toHaveBeenCalled()
  })

  it('keeps loading distinct from signed out across a state change', () => {
    // The regression that matters: an already-signed-in user must never see the
    // login prompt on the way to their page.
    const { rerender } = render(guarded(CONFIGURED, 'loading'))
    expect(screen.getByTestId('auth-loading')).toBeTruthy()

    rerender(guarded(CONFIGURED, 'authenticated'))

    expect(screen.getByTestId('secret')).toBeTruthy()
    expect(screen.queryByTestId('auth-required')).toBeNull()
  })

  it('explains an unconfigured build instead of asking for a login that cannot work', () => {
    renderGuarded({ kind: 'unconfigured', missing: ['VITE_AUTH0_DOMAIN'] }, 'unauthenticated')

    expect(screen.getByTestId('auth-config-missing')).toBeTruthy()
    expect(screen.getByText('VITE_AUTH0_DOMAIN')).toBeTruthy()
    // A login button here would be a dead end: there is no way to sign in.
    expect(screen.queryByTestId('login-controls')).toBeNull()
    expect(screen.queryByTestId('secret')).toBeNull()
  })

  it('treats sandbox mode as a working way to sign in, not the unconfigured case', () => {
    // The regression this task exists to fix: sandbox mode has no
    // `VITE_AUTH0_*` variables by design, and used to read exactly like a
    // genuinely unconfigured build, taking `/`, `/account` and `/admin` down
    // for the whole Playwright suite.
    renderGuarded({ kind: 'sandbox' }, 'authenticated')

    expect(screen.getByTestId('secret')).toBeTruthy()
    expect(screen.queryByTestId('auth-config-missing')).toBeNull()
  })
})

describe('LoginControls', () => {
  function renderControls(returnTo?: string) {
    return render(
      <SessionContext value={sessionState('unauthenticated')}>
        <LoginControls returnTo={returnTo} />
      </SessionContext>,
    )
  }

  it('offers both Google and email/password', () => {
    renderControls()

    expect(screen.getByTestId('login-google')).toBeTruthy()
    expect(screen.getByTestId('login-email')).toBeTruthy()
  })

  it('sends the Google button with the google-oauth2 connection', () => {
    renderControls()

    fireEvent.click(screen.getByTestId('login-google'))

    expect(login).toHaveBeenCalledTimes(1)
    // The exact string the provisioning script enables on the SPA client. A
    // typo here degrades silently into the generic login screen rather than
    // erroring, so it is asserted rather than eyeballed.
    expect(login.mock.calls[0][0].connection).toBe('google-oauth2')
  })

  it('sends the email button with no connection pinned', () => {
    renderControls()

    fireEvent.click(screen.getByTestId('login-email'))

    // No `connection` means Auth0's own screen, which offers the database
    // connection *and* Google — so neither button can strand a user who picked
    // the other method last time.
    expect(login.mock.calls[0][0].connection).toBeUndefined()
  })

  it('remembers where to come back to', () => {
    renderControls('/s/abc123')

    fireEvent.click(screen.getByTestId('login-email'))

    expect(login.mock.calls[0][0].returnTo).toBe('/s/abc123')
  })

  it('defaults returnTo to the current location', () => {
    renderControls()

    fireEvent.click(screen.getByTestId('login-google'))

    expect(login.mock.calls[0][0].returnTo).toBe(window.location.pathname)
  })
})

describe('LogoutButton', () => {
  it('calls session logout', () => {
    render(
      <SessionContext value={sessionState('authenticated')}>
        <LogoutButton />
      </SessionContext>,
    )

    fireEvent.click(screen.getByTestId('logout'))

    expect(logout).toHaveBeenCalledTimes(1)
  })
})
