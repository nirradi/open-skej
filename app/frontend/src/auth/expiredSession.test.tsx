// @vitest-environment jsdom
/**
 * The acceptance test for task 5.4: a session that ends *mid-session*, on a
 * guarded route, must fall through to the login controls rather than leaving
 * a members-only screen on show with no way back in.
 *
 * This is deliberately not shaped like `ProtectedRoute.test.tsx`, which
 * supplies `SessionContext` directly and is right to for its own purpose
 * (exercising the gate in isolation). That shape cannot fail against the bug
 * this task fixes: before the fix, `client.ts` already turned a failed token
 * acquisition into an `unauthenticated` *result* — nothing was wrong with the
 * value — the break was that nothing told `useSession()` about it, so a test
 * that hands the gate a `SessionContext` value never touches the gap at all.
 * Closing it requires the real `SandboxAuthProvider` reading the real
 * module-level store in `client.ts`, with only `fetch` mocked underneath —
 * so this test would have failed on the pre-fix code, which is the point.
 *
 * `sandboxToken.ts`'s own expiry cache is what turns a single request into a
 * mid-session one: the first mint is deliberately given a lifetime inside the
 * client's safety margin, so the *second* request re-mints rather than
 * reusing it, and that second mint is the one made to fail — simulating a
 * revoked grant or a rotated key without a real Auth0 tenant.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useCallback, useEffect, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { listResourceBookings } from '../api'
import { AuthModeContext } from './authConfigContext'
import { ProtectedRoute } from './ProtectedRoute'
import { SandboxAuthProvider, SANDBOX_SIGNED_IN_STORAGE_KEY } from './SandboxAuthProvider'
import { SANDBOX_SUB_STORAGE_KEY } from './sandboxToken'

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'
const SUB = 'sandbox|expiring-session'

/** A JWT-shaped string carrying the given `exp` (epoch seconds) — signature unchecked by the provider. */
function fakeToken(exp: number, sub: string): string {
  const header = btoa(JSON.stringify({ alg: 'RS256', typ: 'JWT' }))
  const payload = btoa(JSON.stringify({ sub, exp }))
  return `${header}.${payload}.signature`
}

const fetchMock = vi.fn<typeof fetch>()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  fetchMock.mockReset()
  window.localStorage.clear()
  // Already signed in before render — establishing "signed in" is the
  // precondition of this test, not something it exercises.
  window.localStorage.setItem(SANDBOX_SIGNED_IN_STORAGE_KEY, 'true')
  window.localStorage.setItem(SANDBOX_SUB_STORAGE_KEY, SUB)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

/** Fetches on mount, and again on demand — the shape of any real guarded panel. */
function GuardedScreen() {
  const [status, setStatus] = useState('idle')

  const load = useCallback(() => {
    void listResourceBookings(PUBLIC_ID, 1, new Date(), new Date()).then((result) => {
      setStatus(result.outcome)
    })
  }, [])

  useEffect(load, [load])

  return (
    <div>
      <p data-testid="guarded-content">{status}</p>
      <button type="button" data-testid="refetch" onClick={load}>
        refetch
      </button>
    </div>
  )
}

function renderGuardedApp() {
  return render(
    <AuthModeContext value={{ kind: 'sandbox' }}>
      <SandboxAuthProvider>
        <ProtectedRoute>
          <GuardedScreen />
        </ProtectedRoute>
      </SandboxAuthProvider>
    </AuthModeContext>,
  )
}

describe('a session that ends mid-session, on a guarded route', () => {
  it('falls through to the login controls, and does not call the token provider again on its own', async () => {
    const nowSeconds = Math.floor(Date.now() / 1000)
    // Minted "valid" — `exp` is in the future — but inside the client's own
    // 60-second safety margin, so the *next* request re-mints instead of
    // reusing it. That is what turns this into a mid-session failure rather
    // than a first-request one.
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ access_token: fakeToken(nowSeconds + 30, SUB) }),
    } as Response)
    fetchMock.mockResolvedValueOnce({ ok: true, status: 200, json: async () => [] } as Response)

    renderGuardedApp()

    // Signed in, on the guarded screen, already fetched successfully once —
    // proving this starts from genuinely signed-in state is the point.
    await waitFor(() => expect(screen.getByTestId('guarded-content').textContent).toBe('ok'))
    expect(screen.queryByTestId('login-controls')).toBeNull()

    // The next mint fails outright — a revoked grant or a rotated key, the
    // same shape as a real Auth0 session dying mid-use.
    fetchMock.mockResolvedValueOnce({ ok: false, status: 401 } as Response)

    fireEvent.click(screen.getByTestId('refetch'))

    await waitFor(() => expect(screen.getByTestId('login-controls')).toBeTruthy())
    // The guarded content is gone from the DOM, not merely hidden — the same
    // property `ProtectedRoute.test.tsx` asserts for its own gate.
    expect(screen.queryByTestId('guarded-content')).toBeNull()

    const callsAtFallthrough = fetchMock.mock.calls.length
    expect(callsAtFallthrough).toBe(3)

    // No-loop, asserted directly rather than inferred from the screen alone:
    // `GuardedScreen` is unmounted and nothing replaced it, so nothing is left
    // to call the token provider again — a delayed automatic retry would show
    // up here as a fourth call.
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(fetchMock.mock.calls.length).toBe(callsAtFallthrough)
  })
})
