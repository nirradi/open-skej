// @vitest-environment jsdom
/**
 * Tests for `/s/{public_id}`, the cold link-holder screen.
 *
 * This is the only route a stranger reaches, so the coverage here is shaped by
 * *who can arrive* rather than by which branches exist:
 *
 * - Someone with no Auth0 configuration at all — a developer with an unset
 *   `.env`, in which case there is no `Auth0Provider` in the tree and calling
 *   `useAuth0()` would crash. The test asserts the hook is never called, not
 *   merely that a notice renders, because a component that calls the hook and
 *   then discards the result passes the weaker assertion and crashes in
 *   production.
 * - A signed-out visitor holding a forwarded link, who must get a coherent
 *   sign-in card that returns them **to this URL** afterwards. Losing the
 *   `returnTo` would strand them on the calendar with no way back to the only
 *   handle to the Space that exists.
 * - Each of the four preview statuses.
 * - Someone whose link resolves to nothing, where the copy is a **security**
 *   question rather than a wording one — see the 404 tests below.
 *
 * Both `../api` and `@auth0/auth0-react` are mocked wholesale: no network, no
 * tenant, no redirect.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuth0 } from '@auth0/auth0-react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { previewSpace, requestAccess } from '../api'
import type { AccessRequest, ApiOk, PreviewStatus, SpacePreview } from '../api'
import { AuthConfigContext } from '../auth'
import type { Auth0ConfigResult } from '../auth'
import { SpacePage } from './SpacePage'

vi.mock('@auth0/auth0-react', () => ({ useAuth0: vi.fn() }))
vi.mock('../api', () => ({ previewSpace: vi.fn(), requestAccess: vi.fn() }))

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'

const loginWithRedirect = vi.fn()

const CONFIG_OK: Auth0ConfigResult = {
  status: 'ok',
  config: {
    domain: 'tenant.example.com',
    clientId: 'client-id',
    audience: 'https://api.open-skej.dev',
  },
}

const CONFIG_MISSING: Auth0ConfigResult = { status: 'missing', missing: ['VITE_AUTH0_DOMAIN'] }

function auth0State(state: { isLoading?: boolean; isAuthenticated?: boolean }) {
  vi.mocked(useAuth0).mockReturnValue({
    isLoading: false,
    isAuthenticated: true,
    loginWithRedirect,
    ...state,
  } as unknown as ReturnType<typeof useAuth0>)
}

function makePreview(overrides: Partial<SpacePreview> = {}): SpacePreview {
  return {
    public_id: PUBLIC_ID,
    name: 'Tennis Court',
    description: 'The one by the car park',
    status: 'none',
    ...overrides,
  }
}

function ok<T>(data: T): ApiOk<T> {
  return { outcome: 'ok', data }
}

const CREATED_REQUEST: AccessRequest = {
  id: 11,
  user_id: 4,
  email: 'bob@example.com',
  name: 'Bob',
  status: 'pending',
  message: null,
  created_at: '2026-07-03T09:00:00Z',
  decided_at: null,
  decided_by_user_id: null,
}

/**
 * Renders the route at `/s/{PUBLIC_ID}`.
 *
 * `/` is mounted too, and with a marker rather than the real calendar, so that
 * the member redirect can be asserted as *arriving somewhere* instead of merely
 * as the preview disappearing.
 */
function renderRoute(config: Auth0ConfigResult = CONFIG_OK) {
  return render(
    <AuthConfigContext value={config}>
      <MemoryRouter initialEntries={[`/s/${PUBLIC_ID}`]}>
        <Routes>
          <Route path="/" element={<p data-testid="calendar">Calendar</p>} />
          <Route path="/s/:publicId" element={<SpacePage />} />
        </Routes>
      </MemoryRouter>
    </AuthConfigContext>,
  )
}

/** Renders with the preview already resolved to one status. */
async function renderWithStatus(status: PreviewStatus) {
  vi.mocked(previewSpace).mockResolvedValue(ok(makePreview({ status })))
  renderRoute()
  return screen.findByTestId(status === 'member' ? 'calendar' : 'space-preview')
}

beforeEach(() => {
  auth0State({})
  vi.mocked(previewSpace).mockResolvedValue(ok(makePreview()))
  vi.mocked(requestAccess).mockResolvedValue(ok(CREATED_REQUEST))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('before there is a session', () => {
  it('renders the config notice without ever calling useAuth0', () => {
    // The load-bearing assertion is the second one. With `VITE_AUTH0_*` unset
    // there is no `Auth0Provider` in the tree, so the hook does not merely
    // return nothing useful — it throws, taking down the app's only public
    // entry point. This route is not behind `ProtectedRoute`, so the split that
    // keeps the hook out of this state has to be its own.
    renderRoute(CONFIG_MISSING)

    expect(screen.getByTestId('auth-config-missing')).toBeTruthy()
    expect(vi.mocked(useAuth0)).not.toHaveBeenCalled()
  })

  it('waits for the SDK rather than flashing the sign-in card at a member', () => {
    auth0State({ isLoading: true })

    renderRoute()

    expect(screen.getByTestId('space-auth-loading')).toBeTruthy()
    expect(screen.queryByTestId('space-sign-in')).toBeNull()
  })

  it('offers a signed-out visitor a way in, and fetches nothing', () => {
    auth0State({ isAuthenticated: false })

    renderRoute()

    expect(screen.getByTestId('space-sign-in')).toBeTruthy()
    // `GET /preview` is authenticated, so calling it here would produce a 401
    // and an error card in place of the invitation to sign in.
    expect(vi.mocked(previewSpace)).not.toHaveBeenCalled()
  })

  it('does not disclose the Space to a signed-out visitor', () => {
    // Not a copy detail: the preview is behind `get_current_user`, so there is
    // nothing to render here even if we wanted to. A card that claimed to
    // describe the Space would be inventing it.
    auth0State({ isAuthenticated: false })

    renderRoute()

    expect(screen.queryByText('Tennis Court')).toBeNull()
  })

  it('sends the visitor back to this exact URL after login', () => {
    auth0State({ isAuthenticated: false })
    renderRoute()

    fireEvent.click(screen.getByTestId('login-email'))

    expect(loginWithRedirect).toHaveBeenCalledWith(
      expect.objectContaining({ appState: { returnTo: `/s/${PUBLIC_ID}` } }),
    )
  })
})

describe('the four statuses', () => {
  it('shows a loading state while the preview is in flight', () => {
    vi.mocked(previewSpace).mockReturnValue(new Promise(() => {}))

    renderRoute()

    expect(screen.getByTestId('space-loading')).toBeTruthy()
  })

  it('offers access to someone with no relationship to the Space', async () => {
    await renderWithStatus('none')

    expect(screen.getByTestId('space-name').textContent).toBe('Tennis Court')
    expect(screen.getByTestId('space-description').textContent).toContain('car park')
    expect(screen.getByTestId('space-status-none')).toBeTruthy()
    expect(screen.getByTestId('request-access')).toBeTruthy()
  })

  it('tells a pending requester to wait, and offers no second request', async () => {
    await renderWithStatus('pending')

    expect(screen.getByTestId('space-status-pending')).toBeTruthy()
    // A second pending request is rejected by a partial unique index, so the
    // button would only ever produce a 409.
    expect(screen.queryByTestId('request-access-form')).toBeNull()
  })

  it('lets a denied user ask again', async () => {
    // Deliberate, and it mirrors the schema: only *pending* requests are unique
    // per user and a denied row is kept as history, so asking again is allowed
    // rather than tolerated.
    await renderWithStatus('denied')

    expect(screen.getByTestId('space-status-denied')).toBeTruthy()
    expect(screen.getByTestId('request-access').textContent).toContain('Ask again')
  })

  it('redirects a member into the Space instead of showing them the door', async () => {
    await renderWithStatus('member')

    expect(screen.getByTestId('calendar')).toBeTruthy()
    expect(screen.queryByTestId('space-preview')).toBeNull()
  })

  it('renders a Space with no description without an empty paragraph', async () => {
    vi.mocked(previewSpace).mockResolvedValue(ok(makePreview({ description: null })))

    renderRoute()

    await screen.findByTestId('space-preview')
    expect(screen.queryByTestId('space-description')).toBeNull()
  })
})

describe('a link that resolves to nothing', () => {
  beforeEach(() => {
    vi.mocked(previewSpace).mockResolvedValue({
      outcome: 'not_found',
      message: "We couldn't find that.",
    })
  })

  it('says the link does not work', async () => {
    renderRoute()

    expect(await screen.findByTestId('space-not-found')).toBeTruthy()
  })

  it('never implies the Space exists', async () => {
    // **This is a security assertion, not a copy nit.** `require_space_role`
    // answers 404 rather than 403 so that an unguessable `public_id` cannot be
    // confirmed by probing — a 403 would turn every forwarded link into an
    // oracle for whether it is still live. Copy that says "you don't have
    // access" or "ask an admin to let you in" hands that back and undoes the
    // status code the backend is spending.
    renderRoute()

    const card = await screen.findByTestId('space-not-found')
    expect(card.textContent).not.toMatch(/access|permission|member|private|exists|admin/i)
  })

  it('offers no way to request access to a Space it cannot confirm', async () => {
    renderRoute()

    await screen.findByTestId('space-not-found')
    expect(screen.queryByTestId('request-access')).toBeNull()
  })
})

describe('requesting access', () => {
  it('sends the note and moves the user to pending', async () => {
    await renderWithStatus('none')

    fireEvent.change(screen.getByTestId('access-message'), {
      target: { value: "  I'm on the Tuesday team  " },
    })
    fireEvent.click(screen.getByTestId('request-access'))

    await screen.findByTestId('space-status-pending')
    // Trimmed: leading and trailing whitespace from a paste is not part of what
    // the admin is being asked to read.
    expect(vi.mocked(requestAccess)).toHaveBeenCalledWith(PUBLIC_ID, "I'm on the Tuesday team")
  })

  it('sends null rather than an empty string when no note is written', async () => {
    await renderWithStatus('none')

    fireEvent.click(screen.getByTestId('request-access'))

    await waitFor(() => expect(vi.mocked(requestAccess)).toHaveBeenCalledWith(PUBLIC_ID, null))
  })

  it("shows the server's own sentence when the request is refused", async () => {
    // The three refusals — archived, already a member, already pending — are
    // distinguished only in prose, so the sentence *is* the answer. Replacing it
    // with generic copy would leave the user clicking the same button forever.
    vi.mocked(requestAccess).mockResolvedValue({
      outcome: 'conflict',
      message: 'This Space is archived and can no longer be changed.',
    })
    await renderWithStatus('none')

    fireEvent.click(screen.getByTestId('request-access'))

    const error = await screen.findByTestId('request-access-error')
    expect(error.textContent).toContain('archived')
  })

  it('keeps the typed note on screen when the request fails', async () => {
    vi.mocked(requestAccess).mockResolvedValue({
      outcome: 'failed',
      message: 'The network went away.',
    })
    await renderWithStatus('none')

    fireEvent.change(screen.getByTestId('access-message'), { target: { value: 'Please let me in' } })
    fireEvent.click(screen.getByTestId('request-access'))

    await screen.findByTestId('request-access-error')
    // Losing someone's explanation to a dropped connection means retyping it,
    // and the note is the whole reason this is a form and not a button.
    expect((screen.getByTestId('access-message') as HTMLTextAreaElement).value).toBe(
      'Please let me in',
    )
  })

  it('disables the button while the request is in flight', async () => {
    vi.mocked(requestAccess).mockReturnValue(new Promise(() => {}))
    await renderWithStatus('none')

    fireEvent.click(screen.getByTestId('request-access'))

    await waitFor(() =>
      expect((screen.getByTestId('request-access') as HTMLButtonElement).disabled).toBe(true),
    )
  })
})

describe('when the preview cannot be loaded', () => {
  it('reports a failure rather than an empty Space', async () => {
    vi.mocked(previewSpace).mockResolvedValue({
      outcome: 'failed',
      message: 'The network went away.',
    })

    renderRoute()

    const error = await screen.findByTestId('space-error')
    expect(error.textContent).toContain('The network went away.')
  })

  it('offers sign-in, not a dead end, when the session turns out to be stale', async () => {
    // The SDK said we were signed in and the server disagreed. Rendering this as
    // a generic error would leave a share link apparently broken for a reason
    // the user could have fixed in one click.
    vi.mocked(previewSpace).mockResolvedValue({
      outcome: 'unauthenticated',
      message: 'Your session has expired. Please sign in again.',
    })

    renderRoute()

    expect(await screen.findByTestId('space-sign-in')).toBeTruthy()
    expect(screen.getByTestId('login-controls')).toBeTruthy()
  })
})
