/**
 * Tests for the api client's auth seam: the injected token and the three access
 * outcomes.
 *
 * Kept in its own file rather than appended to `spaces.test.ts` or
 * `resourceBookings.test.ts` so that neither has to install a token provider
 * to make its own assertions — if a future change made one mandatory, it
 * would fail there rather than here.
 *
 * The pairs matter: proving each of 401/403/404 lands *somewhere* is much
 * weaker than proving they land somewhere *different*, since collapsing all
 * three back into `failed` — which is what the client did before Stream 2 —
 * would satisfy the first claim.
 *
 * `fetch` is mocked throughout; nothing here reaches Auth0 or a server.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  API_BASE_URL,
  authenticatedRequest,
  accessTokenProviderEpoch,
  clearAccessTokenProviderIf,
  clearSessionLost,
  getCurrentUser,
  getSessionLostSnapshot,
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
  // Several tests here deliberately trigger the `unauthenticated` outcome,
  // which now marks the session-lost store as a side effect — reset it so
  // that side effect does not leak into a later test in this file.
  clearSessionLost()
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

    await authenticatedRequest('/spaces')

    // The state the app is in before the Auth0 SDK has finished initialising.
    // Not an error, and specifically not `Bearer undefined` or `Bearer null`,
    // either of which the backend would try to parse as a token.
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

describe('a deferred teardown survives a reinstall of the same provider', () => {
  /**
   * The StrictMode remount contract, at the level it actually broke.
   *
   * React runs effects child-first, so on a remount the session provider's
   * cleanup lands *before* every child's effect re-runs and before its own
   * reinstall. A teardown that nulls the provider there sends those refetches
   * out with no `Authorization` header, and the server is right to 401 them.
   *
   * This got past two attempts. The first nulled unconditionally. The second
   * compared the *provider* — which works for `AccessTokenBridge`, whose
   * closure is fresh each time, and silently fails for `SandboxAuthProvider`,
   * which reinstalls the very same module-level function, so "still mine" was
   * indistinguishable from "reinstalled since". Hence an install counter, and
   * hence this test using one stable reference: with a fresh closure per
   * install it would pass either way and prove nothing.
   */
  const stableProvider = async () => 'a-jwt'

  it('does not uninstall when the same reference has been reinstalled since', async () => {
    setAccessTokenProvider(stableProvider)
    const epoch = accessTokenProviderEpoch()

    // The remount: cleanup captured `epoch` above, the effect reinstalls, and
    // only then does the deferred clear run.
    setAccessTokenProvider(stableProvider)
    clearAccessTokenProviderIf(epoch)

    fetchMock.mockResolvedValue(jsonResponse(200, { id: 1 }))
    await authenticatedRequest('/me')

    expect(sentHeaders().Authorization).toBe('Bearer a-jwt')
  })

  it('still uninstalls when nothing reinstalled', async () => {
    setAccessTokenProvider(stableProvider)
    clearAccessTokenProviderIf(accessTokenProviderEpoch())

    fetchMock.mockResolvedValue(jsonResponse(200, { id: 1 }))
    await authenticatedRequest('/me')

    expect(sentHeaders()).not.toHaveProperty('Authorization')
  })
})

describe('what does and does not end the session', () => {
  /**
   * The store is what flips `useSession()` to `unauthenticated`, so anything
   * that marks it strands the user on the login controls until they sign in
   * again. That makes *not* marking it on a recoverable failure as
   * load-bearing as marking it on a real one — and the two 401s below are
   * the pair that proves the client tells them apart.
   */
  it('ends the session when a token was sent and refused anyway', async () => {
    // A revoked grant, a rotated signing key, a changed tenant: we proved who
    // we were and the server rejected it. The provider itself is perfectly
    // happy, so this is the one path it cannot discover on its own.
    setAccessTokenProvider(async () => 'a-jwt')
    fetchMock.mockResolvedValue(jsonResponse(401, detail('Signature verification failed')))

    await authenticatedRequest('/me')

    expect(getSessionLostSnapshot()).toBe(true)
  })

  it('does NOT end the session on a 401 for a request that carried no token', async () => {
    // No provider installed, so the request went out anonymously and the
    // server said "you never proved anything" — not "what you proved has
    // stopped being true". This happens routinely while the token source is
    // still being installed at page load; the caller retries and succeeds.
    // Marking the session lost here would turn that recoverable race into a
    // permanent sign-out that clearing browser storage would not even cure,
    // and it took every E2E spec down when this store first shipped.
    setAccessTokenProvider(null)
    fetchMock.mockResolvedValue(jsonResponse(401, detail('Not authenticated')))

    const result = await authenticatedRequest('/me')

    expect(result.outcome).toBe('unauthenticated')
    expect(getSessionLostSnapshot()).toBe(false)
  })

  it('ends the session when the token provider itself rejects', async () => {
    // Declared here rather than reused from the block above: that one is
    // scoped to its own `describe`, and reaching for it would resolve to a
    // `ReferenceError` the client happens to catch as a rejected provider —
    // a test that passes for the wrong reason.
    setAccessTokenProvider(async () => {
      throw Object.assign(new Error('Login required'), { error: 'login_required' })
    })

    await authenticatedRequest('/me')

    expect(getSessionLostSnapshot()).toBe(true)
  })

  it('leaves the session alone on a 403, a 404 and a 500', async () => {
    // None of these is a statement about who the caller is: a 403 means the
    // caller is known and still not allowed, which is the opposite of a dead
    // session. Signing them out would be a bizarre response to a permission
    // error.
    setAccessTokenProvider(async () => 'a-jwt')

    for (const status of [403, 404, 500]) {
      fetchMock.mockResolvedValue(jsonResponse(status, detail('nope')))
      await authenticatedRequest('/me')
      expect(getSessionLostSnapshot()).toBe(false)
    }
  })

  it('clears only when asked, not on a later successful request', async () => {
    // Clearing on success would re-arm silent auth the instant a guarded
    // screen fell through to the login controls — the loop task 5.4 must not
    // reintroduce. Only `login()` calls `clearSessionLost`.
    setAccessTokenProvider(async () => 'a-jwt')
    fetchMock.mockResolvedValue(jsonResponse(401, detail('nope')))
    await authenticatedRequest('/me')
    expect(getSessionLostSnapshot()).toBe(true)

    fetchMock.mockResolvedValue(jsonResponse(200, { id: 1 }))
    await authenticatedRequest('/me')

    expect(getSessionLostSnapshot()).toBe(true)
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
