/**
 * Tests for the api client's auth seam: the injected token and the three access
 * outcomes.
 *
 * Kept in its own file rather than appended to `client.test.ts` so that Stream
 * 1's suite stays exactly as it was — and, more usefully, so that *that* file
 * keeps proving the unauthenticated path by construction. `client.test.ts`
 * never installs a token provider, so if a future change made one mandatory, it
 * would fail there rather than here.
 *
 * The pairs matter for the same reason they do in `client.test.ts`: proving
 * each of 401/403/404 lands *somewhere* is much weaker than proving they land
 * somewhere *different*, since collapsing all three back into `failed` — which
 * is what the client did before this task — would satisfy the first claim.
 *
 * `fetch` is mocked throughout; nothing here reaches Auth0 or a server.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  API_BASE_URL,
  authenticatedRequest,
  cancelBooking,
  getCurrentUser,
  listBookings,
  setAccessTokenProvider,
} from './client'

/** Minimal stand-in for `Response`; the client only reads `ok`, `status`, `json`. */
function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

/** An error response whose body is an HTML page, as a proxy or gateway sends. */
function htmlResponse(status: number): Response {
  return {
    ok: false,
    status,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON at position 0')
    },
  } as unknown as Response
}

/** The `detail`-only body FastAPI produces for `HTTPException` — no `error` key. */
function detail(text: string) {
  return { detail: text }
}

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  fetchMock.mockReset()
  // The provider is module-level state; leaving one installed would silently
  // authenticate the next test file's requests.
  setAccessTokenProvider(null)
})

/** The headers the client actually sent on its first call. */
function sentHeaders(): Record<string, string> {
  return (fetchMock.mock.calls[0]?.[1]?.headers ?? {}) as Record<string, string>
}

describe('the injected token provider', () => {
  it('attaches the token it returns as a bearer header', async () => {
    const provider = vi.fn(async () => 'a-jwt')
    setAccessTokenProvider(provider)
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 1 }))

    await authenticatedRequest('/me')

    expect(provider).toHaveBeenCalledTimes(1)
    expect(sentHeaders().Authorization).toBe('Bearer a-jwt')
  })

  it('sends no Authorization header when no provider is installed', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    await listBookings(new Date('2026-07-20T00:00:00Z'), new Date('2026-07-27T00:00:00Z'))

    // The state Stream 1's booking endpoints run in today, and the state the
    // app is in before the Auth0 SDK has finished initialising. Not an error,
    // and specifically not `Bearer undefined` or `Bearer null`, either of which
    // the backend would try to parse as a token.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(sentHeaders()).not.toHaveProperty('Authorization')
    expect(JSON.stringify(sentHeaders())).not.toContain('Bearer')
  })

  it('asks for a fresh token on every call rather than caching one', async () => {
    // The SDK owns expiry and renewal. Caching a token here would mean holding
    // one past its `exp` and 401ing on a session that is perfectly alive.
    const provider = vi.fn(async () => 'a-jwt')
    setAccessTokenProvider(provider)
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 1 }))

    await getCurrentUser()
    await getCurrentUser()

    expect(provider).toHaveBeenCalledTimes(2)
  })

  it('can be uninstalled, reverting to anonymous requests', async () => {
    setAccessTokenProvider(async () => 'a-jwt')
    setAccessTokenProvider(null)
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 1 }))

    await authenticatedRequest('/me')

    expect(sentHeaders()).not.toHaveProperty('Authorization')
  })

  it('still sends the request when the provider succeeds', async () => {
    setAccessTokenProvider(async () => 'a-jwt')
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 4, email: 'a@b.com' }))

    const result = await getCurrentUser()

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`${API_BASE_URL}/me`)
    if (result.outcome !== 'ok') throw new Error('unreachable')
    expect(result.data.id).toBe(4)
  })
})

describe('a token provider that fails', () => {
  /**
   * The ordinary end of a session, not an exotic failure.
   *
   * `getAccessTokenSilently` rejects with `login_required` whenever the session
   * has lapsed and cannot be renewed behind the scenes. Every signed-in user
   * reaches this eventually.
   */
  const loginRequired = Object.assign(new Error('Login required'), { error: 'login_required' })

  it('resolves to unauthenticated instead of rejecting', async () => {
    setAccessTokenProvider(async () => {
      throw loginRequired
    })

    // Not `expect(...).rejects` — the point is that it does not reject at all.
    let threw: unknown = null
    let outcome: string | undefined
    try {
      outcome = (await authenticatedRequest('/me')).outcome
    } catch (error) {
      threw = error
    }

    expect(threw).toBeNull()
    expect(outcome).toBe('unauthenticated')
  })

  it('survives a provider that throws synchronously', async () => {
    setAccessTokenProvider((() => {
      throw loginRequired
    }) as () => Promise<string>)

    const result = await authenticatedRequest('/me')

    expect(result.outcome).toBe('unauthenticated')
  })

  it('does not send the request at all', async () => {
    setAccessTokenProvider(async () => {
      throw loginRequired
    })

    await authenticatedRequest('/me')

    // An anonymous retry would produce the same 401 one round trip later, and
    // would be indistinguishable at the backend from a deliberate anonymous
    // call — so it must not be sent rather than sent without the header.
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('reports friendly copy rather than the SDK error text', async () => {
    setAccessTokenProvider(async () => {
      throw loginRequired
    })

    const result = await authenticatedRequest('/me')

    if (result.outcome !== 'unauthenticated') throw new Error('unreachable')
    expect(result.message).not.toContain('login_required')
    expect(result.message.length).toBeGreaterThan(0)
  })

  it('is not confused with a failed network call', async () => {
    setAccessTokenProvider(async () => {
      throw loginRequired
    })
    const authFailure = await authenticatedRequest('/me')

    setAccessTokenProvider(async () => 'a-jwt')
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    const networkFailure = await authenticatedRequest('/me')

    // One is fixed by signing in, the other by reconnecting. A UI that offered
    // "sign in again" for a dropped Wi-Fi connection would be actively unhelpful.
    expect(authFailure.outcome).toBe('unauthenticated')
    expect(networkFailure.outcome).toBe('failed')
  })
})

describe('401 / 403 / 404 are distinguishable', () => {
  beforeEach(() => {
    setAccessTokenProvider(async () => 'a-jwt')
  })

  it('maps a bare 401 to unauthenticated', async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, detail('Signature verification failed')))

    const result = await authenticatedRequest('/me')

    expect(result.outcome).toBe('unauthenticated')
  })

  it('maps a bare 403 to forbidden', async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, detail('Requires role admin')))

    const result = await authenticatedRequest('/spaces/abc/members')

    expect(result.outcome).toBe('forbidden')
  })

  it('maps a bare 404 to not_found', async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, detail('Space not found')))

    const result = await authenticatedRequest('/spaces/abc')

    expect(result.outcome).toBe('not_found')
  })

  it('keeps all three apart rather than collapsing them into failed', async () => {
    // The regression this whole change exists to prevent. Before it, all three
    // fell through to `failed` with "Something went wrong on our end" — which
    // is wrong for an expired session, wrong for a permission denial, and wrong
    // for a Space that does not exist.
    const outcomes: string[] = []
    for (const status of [401, 403, 404]) {
      fetchMock.mockResolvedValue(jsonResponse(status, detail('nope')))
      outcomes.push((await authenticatedRequest('/spaces/abc')).outcome)
    }

    expect(outcomes).toEqual(['unauthenticated', 'forbidden', 'not_found'])
    expect(new Set(outcomes).size).toBe(3)
    expect(outcomes).not.toContain('failed')
  })

  it('does not leak the server detail into user-facing copy', async () => {
    // FastAPI puts the token-rejection reason in `detail`. That is diagnostics
    // for whoever holds a bad token, not copy for someone who left a tab open.
    fetchMock.mockResolvedValue(jsonResponse(401, detail('Invalid issuer claim')))

    const result = await authenticatedRequest('/me')

    if (result.outcome !== 'unauthenticated') throw new Error('unreachable')
    expect(result.message).not.toContain('issuer')
  })

  it('says nothing about access in the 404 copy', async () => {
    // `require_space_role` returns 404 rather than 403 precisely so that the
    // existence of a Space is not confirmed to a non-member. Copy along the
    // lines of "you don't have access to this Space" would hand back exactly
    // the fact the status code is spending itself to hide.
    fetchMock.mockResolvedValue(jsonResponse(404, detail('Space not found')))

    const result = await authenticatedRequest('/spaces/abc')

    if (result.outcome !== 'not_found') throw new Error('unreachable')
    expect(result.message).not.toMatch(/permission|access|allowed|member/i)
  })

  it('reads the status even when the error body is not JSON', async () => {
    // A gateway answering a 401 with an HTML page has still said "401". Falling
    // back to `failed` here would tell the user to retry a request that will
    // fail identically until they sign in.
    fetchMock.mockResolvedValue(htmlResponse(401))

    const result = await authenticatedRequest('/me')

    expect(result.outcome).toBe('unauthenticated')
  })

  it('still maps a 500 to failed', async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, detail('boom')))

    expect((await authenticatedRequest('/me')).outcome).toBe('failed')
  })

  it('still maps a 422 to invalid_request', async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, { detail: [{ loc: ['body'], msg: 'required' }] }))

    expect((await authenticatedRequest('/spaces')).outcome).toBe('invalid_request')
  })
})

describe('the booking endpoints are unchanged by all this', () => {
  it("keeps cancelBooking's discriminated 404 as not_found, not the status-derived one", async () => {
    // Both are 404 and both end up as `outcome: 'not_found'` — but they must
    // arrive by different routes, because the discriminated one carries the
    // server's own friendly copy and the status-derived one deliberately does
    // not. If the status check ever ran first, this message would be replaced.
    const message = 'That booking no longer exists.'
    fetchMock.mockResolvedValue(jsonResponse(404, { error: 'not_found', message }))

    const result = await cancelBooking(999)

    expect(result).toEqual({ outcome: 'not_found', message })
  })

  it('reports a bare 401 on a booking route as failed, exactly as before', async () => {
    // The booking endpoints are still Stream 1's single-user contract, so their
    // unions model no auth outcomes. A 401 from one means the deployment is
    // wrong, not that the user's session lapsed — and `failed` is what it
    // resolved to before this task, so nothing downstream changed.
    fetchMock.mockResolvedValue(jsonResponse(401, detail('Missing bearer token')))

    const result = await listBookings(new Date('2026-07-20Z'), new Date('2026-07-27Z'))

    expect(result.outcome).toBe('failed')
    if (result.outcome !== 'failed') throw new Error('unreachable')
    expect(String(result.cause)).toContain('unauthenticated')
  })

  it('carries the token on booking calls too, so Stream 4 needs no client change', async () => {
    setAccessTokenProvider(async () => 'a-jwt')
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    await listBookings(new Date('2026-07-20Z'), new Date('2026-07-27Z'))

    expect(sentHeaders().Authorization).toBe('Bearer a-jwt')
  })
})
