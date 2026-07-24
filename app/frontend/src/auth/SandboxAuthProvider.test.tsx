// @vitest-environment jsdom
/**
 * Tests for the sandbox-auth provider component.
 *
 * Token-minting and caching behaviour is covered in `sandboxToken.test.ts`;
 * this file only checks the wiring — that mounting the component installs a
 * provider the api client actually uses. `fetch` is mocked throughout.
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { listBookings, setAccessTokenProvider } from '../api'
import { SandboxAuthProvider } from './SandboxAuthProvider'
import { SANDBOX_SUB_STORAGE_KEY } from './sandboxToken'

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

describe('SandboxAuthProvider', () => {
  it('renders its children', () => {
    render(
      <SandboxAuthProvider>
        <p data-testid="child">The calendar</p>
      </SandboxAuthProvider>,
    )
    expect(screen.getByTestId('child')).toBeTruthy()
  })

  it('installs a token provider the api client uses for outgoing requests', async () => {
    window.localStorage.setItem(SANDBOX_SUB_STORAGE_KEY, 'sandbox|component-wiring')
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
