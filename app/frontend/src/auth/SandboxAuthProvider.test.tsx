// @vitest-environment jsdom
/**
 * Tests for the sandbox-auth provider component.
 *
 * Token-minting and caching behaviour is covered in `sandboxToken.test.ts`;
 * this file checks the wiring — that the component's session state gates
 * whether the token provider it installs actually authenticates a request,
 * and that `login()`/`logout()` flip between the two. `fetch` is mocked
 * throughout.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { listBookings, setAccessTokenProvider } from '../api'
import { SANDBOX_SIGNED_IN_STORAGE_KEY, SandboxAuthProvider } from './SandboxAuthProvider'
import { SANDBOX_SUB_STORAGE_KEY } from './sandboxToken'
import { useSession } from './session'

/** A JWT-shaped string carrying the given `exp` (epoch seconds) — signature unchecked by the provider. */
function fakeToken(exp: number, sub: string): string {
  const header = btoa(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))
  const payload = btoa(JSON.stringify({ sub, exp }))
  return `${header}.${payload}.signature`
}

const HOUR_FROM_NOW = () => Math.floor(Date.now() / 1000) + 3600

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  fetchMock.mockReset()
  window.localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  setAccessTokenProvider(null)
})

/** Reads `useSession().status` onto the DOM and offers buttons that call `login`/`logout`. */
function SessionProbe() {
  const { status, login, logout } = useSession()
  return (
    <div>
      <p data-testid="session-status">{status}</p>
      <button type="button" data-testid="do-login" onClick={() => login()}>
        login
      </button>
      <button type="button" data-testid="do-logout" onClick={() => logout()}>
        logout
      </button>
    </div>
  )
}

describe('SandboxAuthProvider', () => {
  it('renders its children', () => {
    render(
      <SandboxAuthProvider>
        <p data-testid="child">The calendar</p>
      </SandboxAuthProvider>,
    )
    expect(screen.getByTestId('child')).toBeTruthy()
  })

  it('reports unauthenticated when no identity has been committed', () => {
    render(
      <SandboxAuthProvider>
        <SessionProbe />
      </SandboxAuthProvider>,
    )

    expect(screen.getByTestId('session-status').textContent).toBe('unauthenticated')
  })

  it('reports authenticated when the signed-in flag is already set', () => {
    window.localStorage.setItem(SANDBOX_SIGNED_IN_STORAGE_KEY, 'true')

    render(
      <SandboxAuthProvider>
        <SessionProbe />
      </SandboxAuthProvider>,
    )

    expect(screen.getByTestId('session-status').textContent).toBe('authenticated')
  })

  it('flips to authenticated when login() is called, and back on logout()', () => {
    render(
      <SandboxAuthProvider>
        <SessionProbe />
      </SandboxAuthProvider>,
    )

    expect(screen.getByTestId('session-status').textContent).toBe('unauthenticated')

    fireEvent.click(screen.getByTestId('do-login'))
    expect(screen.getByTestId('session-status').textContent).toBe('authenticated')
    expect(window.localStorage.getItem(SANDBOX_SIGNED_IN_STORAGE_KEY)).toBe('true')

    fireEvent.click(screen.getByTestId('do-logout'))
    expect(screen.getByTestId('session-status').textContent).toBe('unauthenticated')
    expect(window.localStorage.getItem(SANDBOX_SIGNED_IN_STORAGE_KEY)).toBeNull()
  })

  it('rejects outgoing requests before any identity is committed', async () => {
    // Mirrors a real Auth0 session with nobody signed in: the api client must
    // see the same `unauthenticated` shape a rejected `getAccessTokenSilently`
    // produces, not silently authenticate as whichever sub happens to sit in
    // `localStorage`.
    window.localStorage.setItem(SANDBOX_SUB_STORAGE_KEY, 'sandbox|component-wiring')

    render(
      <SandboxAuthProvider>
        <p>rendered</p>
      </SandboxAuthProvider>,
    )

    const result = await listBookings(new Date(), new Date())

    expect(fetchMock).not.toHaveBeenCalled()
    expect(result.outcome).toBe('failed')
  })

  it('installs a token provider the api client uses once signed in', async () => {
    window.localStorage.setItem(SANDBOX_SUB_STORAGE_KEY, 'sandbox|component-wiring')
    window.localStorage.setItem(SANDBOX_SIGNED_IN_STORAGE_KEY, 'true')
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ access_token: fakeToken(HOUR_FROM_NOW(), 'sandbox|component-wiring') }),
    } as Response)
    fetchMock.mockResolvedValueOnce({ ok: true, status: 200, json: async () => [] } as Response)

    render(
      <SandboxAuthProvider>
        <p>rendered</p>
      </SandboxAuthProvider>,
    )

    await listBookings(new Date(), new Date())

    const bookingsCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/bookings?'))
    expect(bookingsCall).toBeDefined()
    const headers = bookingsCall?.[1]?.headers as Record<string, string>
    expect(headers.Authorization).toMatch(/^Bearer /)
  })
})
