// @vitest-environment jsdom
/**
 * Tests for `/s/{public_id}`, the cold link-holder screen.
 *
 * This is the only route a stranger reaches, so the coverage here is shaped by
 * *who can arrive* rather than by which branches exist:
 *
 * - Someone with no way to sign in at all — a developer with an unset `.env`
 *   and sandbox mode off, in which case there is no session provider in the
 *   tree and reading the session would crash. The unconfigured case is
 *   asserted by mode alone; nothing here needs to spy on a hook, since
 *   `SpacePage` reads `useAuthMode()` before ever touching `useSession()`.
 * - A signed-out visitor holding a forwarded link, who must get a coherent
 *   sign-in card that returns them **to this URL** afterwards. Losing the
 *   `returnTo` would strand them on the Space list with no way back to the
 *   only handle to the Space that exists.
 * - Each of the four preview statuses.
 * - Someone whose link resolves to nothing, where the copy is a **security**
 *   question rather than a wording one — see the 404 tests below.
 *
 * `../api` is mocked wholesale: no network. The session and auth-mode
 * contexts are supplied directly rather than through a real `AuthProvider` —
 * see `ProtectedRoute.test.tsx` for why that is the shape this task's
 * abstraction is for.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { listResources, previewSpace, requestAccess } from '../api'
import type { AccessRequest, ApiOk, PreviewStatus, Resource, SpacePreview } from '../api'
import { AuthModeContext, SessionContext } from '../auth'
import type { AuthMode, Session, SessionStatus } from '../auth'
import { SpacePage } from './SpacePage'

vi.mock('../api', () => ({ previewSpace: vi.fn(), requestAccess: vi.fn(), listResources: vi.fn() }))

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'

const login = vi.fn()
const logout = vi.fn()

function sessionState(status: SessionStatus): Session {
  return { status, login, logout }
}

const MODE_CONFIGURED: AuthMode = { kind: 'auth0' }
const MODE_UNCONFIGURED: AuthMode = { kind: 'unconfigured', missing: ['VITE_AUTH0_DOMAIN'] }

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

const RESOURCE: Resource = {
  id: 7,
  name: 'Court 1',
  opens_at: null,
  closes_at: null,
  slot_minutes: null,
  created_at: '2026-07-01T00:00:00Z',
  archived_at: null,
}

/** Renders the route at `/s/{PUBLIC_ID}`. */
function renderRoute(mode: AuthMode = MODE_CONFIGURED, status: SessionStatus = 'authenticated') {
  return render(
    <AuthModeContext value={mode}>
      <SessionContext value={sessionState(status)}>
        <MemoryRouter initialEntries={[`/s/${PUBLIC_ID}`]}>
          <Routes>
            <Route path="/" element={<p data-testid="space-list">Your Spaces</p>} />
            <Route path="/s/:publicId" element={<SpacePage />} />
          </Routes>
        </MemoryRouter>
      </SessionContext>
    </AuthModeContext>,
  )
}

/** Renders with the preview already resolved to one status. */
async function renderWithStatus(status: PreviewStatus) {
  vi.mocked(previewSpace).mockResolvedValue(ok(makePreview({ status })))
  renderRoute()
  return screen.findByTestId(status === 'member' ? 'resource-list' : 'space-preview')
}

beforeEach(() => {
  vi.mocked(previewSpace).mockResolvedValue(ok(makePreview()))
  vi.mocked(requestAccess).mockResolvedValue(ok(CREATED_REQUEST))
  vi.mocked(listResources).mockResolvedValue(ok([RESOURCE]))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('before there is a session', () => {
  it('renders the unconfigured notice rather than reading a session that does not exist', () => {
    // With neither Auth0 nor sandbox mode configured there is no session
    // provider in the tree, so reading one would crash. This route is not
    // behind `ProtectedRoute`, so the split that keeps the session read out of
    // this state has to be its own — see `SpacePage`'s two-component split.
    renderRoute(MODE_UNCONFIGURED, 'loading')

    expect(screen.getByTestId('auth-config-missing')).toBeTruthy()
  })

  it('waits for the session rather than flashing the sign-in card at a member', () => {
    renderRoute(MODE_CONFIGURED, 'loading')

    expect(screen.getByTestId('space-auth-loading')).toBeTruthy()
    expect(screen.queryByTestId('space-sign-in')).toBeNull()
  })

  it('offers a signed-out visitor a way in, and fetches nothing', () => {
    renderRoute(MODE_CONFIGURED, 'unauthenticated')

    expect(screen.getByTestId('space-sign-in')).toBeTruthy()
    // `GET /preview` is authenticated, so calling it here would produce a 401
    // and an error card in place of the invitation to sign in.
    expect(vi.mocked(previewSpace)).not.toHaveBeenCalled()
  })

  it('does not disclose the Space to a signed-out visitor', () => {
    // Not a copy detail: the preview is behind `get_current_user`, so there is
    // nothing to render here even if we wanted to. A card that claimed to
    // describe the Space would be inventing it.
    renderRoute(MODE_CONFIGURED, 'unauthenticated')

    expect(screen.queryByText('Tennis Court')).toBeNull()
  })

  it('sends the visitor back to this exact URL after login', () => {
    renderRoute(MODE_CONFIGURED, 'unauthenticated')

    fireEvent.click(screen.getByTestId('login-email'))

    expect(login).toHaveBeenCalledWith(
      expect.objectContaining({ returnTo: `/s/${PUBLIC_ID}` }),
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

  it('lands a member in the Space instead of showing them the door', async () => {
    await renderWithStatus('member')

    // Arrives at the Space member view — never the generic Space list `/`
    // redirects to for every other post-login destination.
    expect(screen.getByTestId('space-name').textContent).toBe('Tennis Court')
    expect(screen.queryByTestId('space-preview')).toBeNull()
    expect(screen.queryByTestId('space-list')).toBeNull()
  })

  it('offers a Resource picker onto the Space', async () => {
    await renderWithStatus('member')

    const link = screen.getByTestId(`resource-list-item-${RESOURCE.id}`)
    expect(link.textContent).toBe('Court 1')
    expect(link.getAttribute('href')).toBe(`/s/${PUBLIC_ID}/resources/${RESOURCE.id}`)
  })

  it('renders a sane empty state for a Space with no Resources', async () => {
    // Representable in the schema but never produced by the product — creating
    // a Space auto-creates its first Resource — so this is a defensive
    // rendering, not a reachable one.
    vi.mocked(listResources).mockResolvedValue(ok([]))
    vi.mocked(previewSpace).mockResolvedValue(ok(makePreview({ status: 'member' })))
    renderRoute()

    expect(await screen.findByTestId('resource-list-empty')).toBeTruthy()
  })

  it('reports a failed Resource fetch rather than an empty picker', async () => {
    vi.mocked(listResources).mockResolvedValue({
      outcome: 'failed',
      message: 'The network went away.',
    })
    vi.mocked(previewSpace).mockResolvedValue(ok(makePreview({ status: 'member' })))
    renderRoute()

    const error = await screen.findByTestId('resource-list-error')
    expect(error.textContent).toContain('The network went away.')
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
