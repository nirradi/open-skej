// @vitest-environment jsdom
/**
 * The acceptance tests for task 5.6: a Resource link admits a stranger the
 * same way a Space link does.
 *
 * These drive the **routes**, through `App`, rather than rendering
 * `SpaceAccessGate` directly. What the task changed is which door each URL is
 * mounted behind, and a test that renders the gate itself would pass no
 * matter what `App.tsx` wires up — the wiring is the deliverable here.
 *
 * ## Why the `returnTo` assertions cannot be an E2E test
 *
 * Sandbox mode's `login()` is a synchronous state update that never leaves the
 * page, so the URL does not change across a sandbox sign-in. A Playwright test
 * that signs in at a Resource URL and finds itself still at that Resource URL
 * would therefore pass even if `returnTo` were hardcoded to `/s/{publicId}`,
 * or omitted altogether — it would be asserting that a redirect which never
 * happened did not move it. That is the same dead-test shape task 4.10 shipped
 * and this stream keeps having to call out, so the `returnTo` contract is
 * pinned here, where the value handed to `login` is actually observable.
 *
 * E2E covers what it genuinely can: that the cold and member entry shapes
 * render, in a real browser, against a real backend.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from '../App'
import * as api from '../api'
import { AuthModeContext, SessionContext, type Session } from '../auth'

const PUBLIC_ID = 'aBcDeFgHiJkLmNoPqRsTuV'
const RESOURCE_ID = 3
const RESOURCE_PATH = `/s/${PUBLIC_ID}/resources/${RESOURCE_ID}`
const SPACE_PATH = `/s/${PUBLIC_ID}`

function sessionWith(status: Session['status'], login: Session['login'] = () => {}): Session {
  return { status, login, logout: () => {} }
}

function renderAt(path: string, session: Session) {
  window.history.pushState({}, '', path)
  return render(
    <AuthModeContext value={{ kind: 'sandbox' }}>
      <SessionContext value={session}>
        <App />
      </SessionContext>
    </AuthModeContext>,
  )
}

/** `previewSpace` answering with one of the four statuses the gate branches on. */
function stubPreview(status: 'none' | 'pending' | 'denied' | 'member') {
  vi.spyOn(api, 'previewSpace').mockResolvedValue({
    outcome: 'ok',
    data: { public_id: PUBLIC_ID, name: 'Tennis Court', description: 'Two courts', status },
  })
}

/**
 * The header/calendar fetches behind the member branch; display only.
 *
 * Two Resources, not one: task 5.7 redirects a single-Resource Space straight
 * to its calendar, and these tests are about the door (`SpaceAccessGate`),
 * not that redirect — a one-Resource stub here would send the Space-link
 * tests straight past the picker this suite is asserting on.
 */
function stubMemberScreens() {
  vi.spyOn(api, 'listResources').mockResolvedValue({
    outcome: 'ok',
    data: [
      { id: RESOURCE_ID, name: 'Court 1', created_at: '2026-07-01T00:00:00Z', archived_at: null },
      { id: RESOURCE_ID + 1, name: 'Court 2', created_at: '2026-07-01T00:00:00Z', archived_at: null },
    ],
  })
  vi.spyOn(api, 'getSpace').mockResolvedValue({
    outcome: 'ok',
    data: {
      public_id: PUBLIC_ID,
      name: 'Tennis Court',
      description: null,
      timezone: 'UTC',
      opens_at: null,
      closes_at: null,
      slot_minutes: null,
      max_duration_minutes: null,
      booking_horizon_days: null,
      max_bookings_per_week: null,
      max_bookings_per_month: null,
      created_at: '2026-07-01T00:00:00Z',
      archived_at: null,
      my_role: 'member',
    },
  })
  vi.spyOn(api, 'listResourceBookings').mockResolvedValue({ outcome: 'ok', data: [] })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('a forwarded Resource link', () => {
  it('offers a signed-out visitor the sign-in card, not "you need an account"', async () => {
    renderAt(RESOURCE_PATH, sessionWith('unauthenticated'))

    // The Space link's card, at the Resource URL. `auth-required` is
    // `ProtectedRoute`'s members-only copy and is what used to render here —
    // a dead end for someone who was handed the link.
    await screen.findByTestId('space-sign-in')
    expect(screen.queryByTestId('auth-required')).toBeNull()
  })

  it('returns the visitor to the Resource URL after signing in, not to the Space', async () => {
    // The assertion no E2E can make — see the file docstring. A gate that
    // passed `/s/{publicId}` here would strand the visitor on the picker,
    // having lost the Resource they were actually sent.
    const login = vi.fn()
    renderAt(RESOURCE_PATH, sessionWith('unauthenticated', login))

    await screen.findByTestId('space-sign-in')
    fireEvent.click(screen.getByTestId('login-email'))

    expect(login).toHaveBeenCalledTimes(1)
    expect(login.mock.calls[0][0]).toMatchObject({ returnTo: RESOURCE_PATH })
  })

  it('shows a signed-in non-member the Space preview and the access request', async () => {
    stubPreview('none')
    renderAt(RESOURCE_PATH, sessionWith('authenticated'))

    await screen.findByTestId('space-preview')
    expect(screen.getByTestId('space-status-none')).toBeTruthy()
    expect(screen.getByTestId('request-access')).toBeTruthy()
  })

  it('renders that preview at the Resource URL rather than redirecting to the Space', async () => {
    // Load-bearing: the URL the visitor sits on is what makes approval land
    // them on the Resource they were sent. Redirecting to `/s/{publicId}`
    // would throw that away, and nothing else in the app would put it back.
    stubPreview('none')
    renderAt(RESOURCE_PATH, sessionWith('authenticated'))

    await screen.findByTestId('space-preview')
    expect(window.location.pathname).toBe(RESOURCE_PATH)
  })

  it('spends a 404 that names the link, never the Space', async () => {
    // The one piece of copy here that is a security decision: confirming the
    // id is live would turn every forwarded link into an oracle.
    vi.spyOn(api, 'previewSpace').mockResolvedValue({
      outcome: 'not_found',
      message: 'not found',
    })
    renderAt(RESOURCE_PATH, sessionWith('authenticated'))

    const card = await screen.findByTestId('space-not-found')
    expect(card.textContent).toContain("That link doesn't work")
    expect(card.textContent).not.toMatch(/access|permission|member/i)
  })

  it('lets a member straight through to the calendar', async () => {
    stubPreview('member')
    stubMemberScreens()
    renderAt(RESOURCE_PATH, sessionWith('authenticated'))

    await screen.findByTestId('calendar')
    expect(screen.queryByTestId('space-preview')).toBeNull()
  })
})

describe('the Space link keeps its own behaviour', () => {
  it('returns a signed-out visitor to the Space URL, not a Resource one', async () => {
    // The other half of the non-drift check: one gate now serves both routes,
    // so each has to keep supplying its own `returnTo`.
    const login = vi.fn()
    renderAt(SPACE_PATH, sessionWith('unauthenticated', login))

    await screen.findByTestId('space-sign-in')
    fireEvent.click(screen.getByTestId('login-email'))

    expect(login.mock.calls[0][0]).toMatchObject({ returnTo: SPACE_PATH })
  })

  it('still lands a member on the Resource picker', async () => {
    stubPreview('member')
    stubMemberScreens()
    renderAt(SPACE_PATH, sessionWith('authenticated'))

    await waitFor(() => expect(screen.getByTestId('space-name')).toBeTruthy())
    expect(await screen.findByText('Court 1')).toBeTruthy()
  })
})
