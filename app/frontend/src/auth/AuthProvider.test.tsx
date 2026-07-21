// @vitest-environment jsdom
/**
 * Regression tests for what happens when Auth0 is not configured.
 *
 * These exist because of a bug that no unit test caught and the Playwright
 * suite did. The first version of `AuthProvider` returned `MissingConfigNotice`
 * instead of its children when the `VITE_AUTH0_*` variables were absent —
 * which, in CI, is always. The result was that the entire app disappeared
 * behind a configuration warning and twelve Stream 1 browser tests failed
 * looking for a calendar that was no longer rendered.
 *
 * The lesson is about blast radius rather than about Auth0: a provider sits
 * above everything, so a provider that refuses to render is an outage for code
 * that has no relationship to it. The calendar at `/` is unauthenticated and
 * works fine against no tenant at all.
 *
 * `./config` is mocked rather than leaning on the ambient environment, so the
 * result does not change on a machine that happens to have a `.env.local`.
 */

import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AuthProvider } from './AuthProvider'
import { readAuth0Config } from './config'

vi.mock('./config', () => ({ readAuth0Config: vi.fn() }))

afterEach(() => {
  vi.clearAllMocks()
})

describe('AuthProvider with no Auth0 configuration', () => {
  it('still renders the app', () => {
    // The regression, stated directly.
    vi.mocked(readAuth0Config).mockReturnValue({
      status: 'missing',
      missing: ['VITE_AUTH0_DOMAIN', 'VITE_AUTH0_CLIENT_ID', 'VITE_AUTH0_AUDIENCE'],
    })

    render(
      <AuthProvider>
        <p data-testid="unauthenticated-page">The calendar</p>
      </AuthProvider>,
    )

    expect(screen.getByTestId('unauthenticated-page')).toBeTruthy()
  })

  it('does not put the config notice at the root', () => {
    // The notice belongs on the routes that actually need a tenant — see
    // `ProtectedRoute`, which renders it — not in front of the whole app.
    vi.mocked(readAuth0Config).mockReturnValue({
      status: 'missing',
      missing: ['VITE_AUTH0_DOMAIN'],
    })

    render(
      <AuthProvider>
        <p data-testid="unauthenticated-page">The calendar</p>
      </AuthProvider>,
    )

    expect(screen.queryByTestId('auth-config-missing')).toBeNull()
  })
})
