// @vitest-environment jsdom
/**
 * Tests for the pending access-request queue.
 *
 * Beyond the ordinary loading/empty/error/approve/deny coverage, one assertion
 * here is guarding a **product decision rather than an implementation detail**:
 * there must be no role selector on this screen. Approval grants `member`, full
 * stop, and an admin who wants the new arrival higher promotes them in the
 * members panel afterwards.
 *
 * That is pinned by a test because the "obvious" improvement — a little dropdown
 * next to Approve — is a change someone will reach for, and its cost is not
 * visible from this file. The members route is where the owner-authority and
 * last-owner invariants live; a role picker here would need a second copy of
 * both, which is the shape privilege-escalation bugs arrive in.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { approveAccessRequest, denyAccessRequest, listAccessRequests } from '../api'
import { AccessRequestsPanel } from './AccessRequestsPanel'
import { failed, forbidden, makeAccessRequest, makeSpace, ok } from './fixtures'

vi.mock('../api', () => ({
  listAccessRequests: vi.fn(),
  approveAccessRequest: vi.fn(),
  denyAccessRequest: vi.fn(),
}))

beforeEach(() => {
  vi.mocked(listAccessRequests).mockResolvedValue(ok([]))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPanel(space = makeSpace(), onApproved = vi.fn()) {
  render(<AccessRequestsPanel space={space} onApproved={onApproved} />)
  return { onApproved }
}

describe('AccessRequestsPanel', () => {
  it('shows a loading state before the queue arrives', () => {
    // A promise that never settles: the first paint is the subject, and letting
    // it resolve would race the assertion.
    vi.mocked(listAccessRequests).mockReturnValue(new Promise(() => {}))

    renderPanel()

    expect(screen.getByTestId('requests-loading')).toBeTruthy()
  })

  it('says so when nobody is waiting', async () => {
    renderPanel()

    expect(await screen.findByTestId('requests-empty')).toBeTruthy()
  })

  it('reports an error instead of an empty queue', async () => {
    // The distinction that matters: "nobody is waiting" and "we could not find
    // out" look identical if a failure renders as an empty list, and the admin
    // would never learn there were people to let in.
    vi.mocked(listAccessRequests).mockResolvedValue(failed('The network went away.'))

    renderPanel()

    const error = await screen.findByTestId('requests-error')
    expect(error.textContent).toBe('The network went away.')
    expect(screen.queryByTestId('requests-empty')).toBeNull()
  })

  it('asks the server for pending requests only', async () => {
    renderPanel()
    await screen.findByTestId('requests-empty')

    // Filtering server-side rather than client-side keeps the names of everyone
    // ever refused out of a response this screen never renders.
    expect(vi.mocked(listAccessRequests)).toHaveBeenCalledWith('sp_7f3a9c', { status: 'pending' })
  })

  it('shows who is asking, and what they said', async () => {
    vi.mocked(listAccessRequests).mockResolvedValue(
      ok([makeAccessRequest({ id: 10, message: 'I play on Thursdays' })]),
    )

    renderPanel()

    const row = await screen.findByTestId('request-10')
    expect(row.textContent).toContain('Grace Hopper')
    expect(row.textContent).toContain('grace@example.com')
    expect(screen.getByTestId('request-message-10').textContent).toContain('I play on Thursdays')
  })

  it('offers no role selector — approval grants member by design', async () => {
    vi.mocked(listAccessRequests).mockResolvedValue(ok([makeAccessRequest({ id: 10 })]))

    renderPanel()
    await screen.findByTestId('request-10')

    // Asserted structurally rather than by testid, so that adding a picker under
    // any name still trips this.
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
    expect(screen.getByTestId('request-approve-10')).toBeTruthy()
    expect(screen.getByTestId('request-deny-10')).toBeTruthy()
  })

  it('approves a request and drops it from the queue', async () => {
    vi.mocked(listAccessRequests).mockResolvedValue(ok([makeAccessRequest({ id: 10 })]))
    vi.mocked(approveAccessRequest).mockResolvedValue(ok(makeAccessRequest({ status: 'approved' })))
    const { onApproved } = renderPanel()

    fireEvent.click(await screen.findByTestId('request-approve-10'))

    expect(vi.mocked(approveAccessRequest)).toHaveBeenCalledWith('sp_7f3a9c', 10)
    await vi.waitFor(() => expect(screen.queryByTestId('request-10')).toBeNull())
    // Approving created a membership, so the members list is now stale and has
    // to be told.
    expect(onApproved).toHaveBeenCalledTimes(1)
  })

  it('denies a request without touching the member list', async () => {
    vi.mocked(listAccessRequests).mockResolvedValue(ok([makeAccessRequest({ id: 10 })]))
    vi.mocked(denyAccessRequest).mockResolvedValue(ok(makeAccessRequest({ status: 'denied' })))
    const { onApproved } = renderPanel()

    fireEvent.click(await screen.findByTestId('request-deny-10'))

    expect(vi.mocked(denyAccessRequest)).toHaveBeenCalledWith('sp_7f3a9c', 10)
    await vi.waitFor(() => expect(screen.queryByTestId('request-10')).toBeNull())
    expect(vi.mocked(approveAccessRequest)).not.toHaveBeenCalled()
    // A denial adds nobody, so refetching members would be pure noise.
    expect(onApproved).not.toHaveBeenCalled()
  })

  it('keeps a refused decision next to the row that caused it', async () => {
    // Two admins working at once is enough to make this reachable: the other one
    // can demote you between your page load and your click.
    vi.mocked(listAccessRequests).mockResolvedValue(
      ok([makeAccessRequest({ id: 10 }), makeAccessRequest({ id: 11, email: 'x@example.com' })]),
    )
    vi.mocked(approveAccessRequest).mockResolvedValue(forbidden('You are no longer an admin here.'))
    renderPanel()

    fireEvent.click(await screen.findByTestId('request-approve-10'))

    const error = await screen.findByTestId('request-error-10')
    expect(error.textContent).toBe('You are no longer an admin here.')
    // The row survives a failed decision — it was never decided.
    expect(screen.getByTestId('request-10')).toBeTruthy()
    // And the error is scoped to its own row, not splashed across the panel.
    expect(screen.queryByTestId('request-error-11')).toBeNull()
  })

  it('offers no decisions on an archived Space', async () => {
    vi.mocked(listAccessRequests).mockResolvedValue(ok([makeAccessRequest({ id: 10 })]))

    renderPanel(makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' }))

    const approve = await screen.findByTestId('request-approve-10')
    expect(approve.hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('request-deny-10').hasAttribute('disabled')).toBe(true)
  })
})
