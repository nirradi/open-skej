// @vitest-environment jsdom
/**
 * Tests for the seam between the Auth0 SDK and the api client.
 *
 * This is the piece that would otherwise only be exercised by a real login, so
 * it gets the SDK mocked and the api client observed through `fetch`: the
 * question is not "does the bridge call `setAccessTokenProvider`" but "does a
 * request made by a component underneath it actually carry the token".
 */

import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuth0 } from '@auth0/auth0-react'

import { authenticatedRequest, setAccessTokenProvider } from '../api'
import { AccessTokenBridge } from './AccessTokenBridge'

vi.mock('@auth0/auth0-react', () => ({ useAuth0: vi.fn() }))

const getAccessTokenSilently = vi.fn(async () => 'a-jwt')
const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  vi.mocked(useAuth0).mockReturnValue({
    getAccessTokenSilently,
  } as unknown as ReturnType<typeof useAuth0>)
  fetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ id: 1 }),
  } as Response)
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  setAccessTokenProvider(null)
})

/** A child that fires a request as it mounts, like every real data component. */
function FetchesOnMount() {
  return (
    <p
      data-testid="child"
      ref={() => {
        void authenticatedRequest('/me')
      }}
    >
      child
    </p>
  )
}

describe('AccessTokenBridge', () => {
  it('installs a provider that yields the SDK token', async () => {
    render(
      <AccessTokenBridge>
        <p data-testid="child">child</p>
      </AccessTokenBridge>,
    )
    await screen.findByTestId('child')

    const result = await authenticatedRequest('/me')

    expect(result.outcome).toBe('ok')
    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer a-jwt')
  })

  it('does render its children once the provider is in place', () => {
    // The gate below must not become a permanent one.
    render(
      <AccessTokenBridge>
        <p data-testid="child">child</p>
      </AccessTokenBridge>,
    )

    expect(screen.getByTestId('child')).toBeTruthy()
  })

  it('never lets a child request go out unauthenticated', async () => {
    render(
      <AccessTokenBridge>
        <FetchesOnMount />
      </AccessTokenBridge>,
    )

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    // The claim in full: every request the subtree made, from its very first
    // render, carried a token. Not "eventually settles into carrying one".
    for (const call of fetchMock.mock.calls) {
      const headers = call[1]?.headers as Record<string, string>
      expect(headers.Authorization).toBe('Bearer a-jwt')
    }
  })

  it('uninstalls the provider when it unmounts', async () => {
    const { unmount } = render(
      <AccessTokenBridge>
        <p data-testid="child">child</p>
      </AccessTokenBridge>,
    )
    await screen.findByTestId('child')
    unmount()

    await authenticatedRequest('/me')

    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Record<string, string>
    expect(headers).not.toHaveProperty('Authorization')
  })

  it('passes a rejecting provider through as unauthenticated, not a crash', async () => {
    getAccessTokenSilently.mockRejectedValueOnce(
      Object.assign(new Error('Login required'), { error: 'login_required' }),
    )
    render(
      <AccessTokenBridge>
        <p data-testid="child">child</p>
      </AccessTokenBridge>,
    )
    await screen.findByTestId('child')

    const result = await authenticatedRequest('/me')

    // The bridge deliberately does not catch this: the api client already turns
    // it into an outcome, and a component that has no idea what request was in
    // flight is the wrong place to decide what to do about it.
    expect(result.outcome).toBe('unauthenticated')
    expect(screen.getByTestId('child')).toBeTruthy()
  })
})
