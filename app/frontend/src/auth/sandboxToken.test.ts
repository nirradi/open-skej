// @vitest-environment jsdom
/**
 * Tests for sandbox-mode token minting and caching.
 *
 * `fetch` is mocked throughout — nothing here talks to a real backend. Each
 * test that mints a token uses its own `sub`, since the module-level cache is
 * keyed on `sub` and the module is not reset between tests in this file — a
 * distinct sub is a cache miss, which is the simplest way to get a clean mint
 * without reaching into the module's private state.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SANDBOX_SUB_STORAGE_KEY, getSandboxAccessToken } from './sandboxToken'

/** A JWT-shaped string carrying the given `exp` (epoch seconds) — signature unchecked by this module. */
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
  vi.unstubAllGlobals()
})

/** Queues one `POST /sandbox/token` response. */
function mockSandboxToken(token: string): void {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => ({ access_token: token, token_type: 'bearer' }),
  } as Response)
}

describe('getSandboxAccessToken', () => {
  it('mints a token for the seeded owner when no sub is chosen', async () => {
    const token = fakeToken(HOUR_FROM_NOW(), 'sandbox|owner')
    mockSandboxToken(token)

    await expect(getSandboxAccessToken()).resolves.toBe(token)
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/sandbox/token'),
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ sub: 'sandbox|owner' }) }),
    )
  })

  it('mints a token for whichever sub is in localStorage', async () => {
    window.localStorage.setItem(SANDBOX_SUB_STORAGE_KEY, 'sandbox|admin')
    const token = fakeToken(HOUR_FROM_NOW(), 'sandbox|admin')
    mockSandboxToken(token)

    await expect(getSandboxAccessToken()).resolves.toBe(token)
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/sandbox/token'),
      expect.objectContaining({ body: JSON.stringify({ sub: 'sandbox|admin' }) }),
    )
  })

  it('reuses a cached token instead of minting on every call', async () => {
    window.localStorage.setItem(SANDBOX_SUB_STORAGE_KEY, 'sandbox|cache-reuse')
    mockSandboxToken(fakeToken(HOUR_FROM_NOW(), 'sandbox|cache-reuse'))

    await getSandboxAccessToken()
    await getSandboxAccessToken()

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('re-mints once the cached token is past its safety margin', async () => {
    window.localStorage.setItem(SANDBOX_SUB_STORAGE_KEY, 'sandbox|cache-expiry')
    const almostExpired = Math.floor(Date.now() / 1000) + 30
    const fresh = fakeToken(HOUR_FROM_NOW(), 'sandbox|cache-expiry')
    mockSandboxToken(fakeToken(almostExpired, 'sandbox|cache-expiry'))
    mockSandboxToken(fresh)

    await getSandboxAccessToken()
    const second = await getSandboxAccessToken()

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(second).toBe(fresh)
  })

  it('mints again once the sub changes, even with an unexpired cached token', async () => {
    window.localStorage.setItem(SANDBOX_SUB_STORAGE_KEY, 'sandbox|switch-a')
    mockSandboxToken(fakeToken(HOUR_FROM_NOW(), 'sandbox|switch-a'))
    await getSandboxAccessToken()

    window.localStorage.setItem(SANDBOX_SUB_STORAGE_KEY, 'sandbox|switch-b')
    const second = fakeToken(HOUR_FROM_NOW(), 'sandbox|switch-b')
    mockSandboxToken(second)

    await expect(getSandboxAccessToken()).resolves.toBe(second)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('rejects when the sandbox endpoint answers with a non-2xx status', async () => {
    window.localStorage.setItem(SANDBOX_SUB_STORAGE_KEY, 'sandbox|error-case')
    fetchMock.mockResolvedValueOnce({ ok: false, status: 500 } as Response)

    await expect(getSandboxAccessToken()).rejects.toThrow(/sandbox\/token failed/)
  })
})
