// @vitest-environment jsdom
/**
 * Tests for sending and revoking invitations.
 *
 * Two behaviours here are easy to get backwards, so both are pinned:
 *
 * 1. **A revoked invitation is replaced, not removed.** The server answers with
 *    the revoked row rather than a 204, and its `status` is the evidence the
 *    revocation landed. Dropping the row would erase the record of who invited
 *    whom, which outlives the access it granted.
 * 2. **Only pending invitations offer a Revoke button.** An accepted invitation
 *    has already become a membership, and revoking it would not take that
 *    membership away — the server refuses with a 409 saying exactly that. So the
 *    resolved rows stay visible as history with no button.
 *
 * The copy assertion — that the panel says no email is sent — is also load
 * bearing rather than cosmetic. Nothing is emailed; the admin shares the Space
 * link themselves. An admin who assumes otherwise sits waiting for someone who
 * was never told.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createInvitation, listInvitations, revokeInvitation } from '../api'
import { conflict, failed, forbidden, makeInvitation, makeSpace, ok } from './fixtures'
import { InvitationsPanel } from './InvitationsPanel'

vi.mock('../api', () => ({
  listInvitations: vi.fn(),
  createInvitation: vi.fn(),
  revokeInvitation: vi.fn(),
}))

beforeEach(() => {
  vi.mocked(listInvitations).mockResolvedValue(ok([]))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderPanel(space = makeSpace()) {
  render(<InvitationsPanel space={space} />)
}

describe('InvitationsPanel', () => {
  it('shows a loading state before the list arrives', () => {
    vi.mocked(listInvitations).mockReturnValue(new Promise(() => {}))

    renderPanel()

    expect(screen.getByTestId('invitations-loading')).toBeTruthy()
  })

  it('says so when nobody has been invited', async () => {
    renderPanel()

    expect(await screen.findByTestId('invitations-empty')).toBeTruthy()
  })

  it('reports an error instead of an empty list', async () => {
    vi.mocked(listInvitations).mockResolvedValue(failed('The network went away.'))

    renderPanel()

    const error = await screen.findByTestId('invitations-error')
    expect(error.textContent).toBe('The network went away.')
    expect(screen.queryByTestId('invitations-empty')).toBeNull()
  })

  it('states that no email is sent', () => {
    renderPanel()

    expect(screen.getByTestId('invitations-panel').textContent).toContain('No email is sent')
  })

  it('sends an invitation and shows it immediately', async () => {
    const created = makeInvitation({ id: 20, email: 'alan@example.com', role: 'member' })
    vi.mocked(createInvitation).mockResolvedValue(ok(created))
    renderPanel()
    await screen.findByTestId('invitations-empty')

    fireEvent.change(screen.getByTestId('invite-email'), {
      target: { value: 'alan@example.com' },
    })
    fireEvent.click(screen.getByTestId('invite-submit'))

    expect(vi.mocked(createInvitation)).toHaveBeenCalledWith(
      'sp_7f3a9c',
      'alan@example.com',
      'member',
    )
    expect(await screen.findByTestId('invitation-20')).toBeTruthy()
  })

  it('trims the address before sending it', async () => {
    vi.mocked(createInvitation).mockResolvedValue(ok(makeInvitation()))
    renderPanel()
    await screen.findByTestId('invitations-empty')

    fireEvent.change(screen.getByTestId('invite-email'), {
      target: { value: '  alan@example.com  ' },
    })
    fireEvent.click(screen.getByTestId('invite-submit'))

    // A pasted address routinely carries whitespace, and the server matches
    // addresses exactly — an untrimmed one silently invites nobody.
    expect(vi.mocked(createInvitation)).toHaveBeenCalledWith(
      'sp_7f3a9c',
      'alan@example.com',
      'member',
    )
  })

  it('refuses to send an empty address without asking the server', async () => {
    renderPanel()
    await screen.findByTestId('invitations-empty')

    fireEvent.click(screen.getByTestId('invite-submit'))

    expect(await screen.findByTestId('invite-error')).toBeTruthy()
    expect(vi.mocked(createInvitation)).not.toHaveBeenCalled()
  })

  it('invites at the chosen role', async () => {
    vi.mocked(createInvitation).mockResolvedValue(ok(makeInvitation({ role: 'admin' })))
    renderPanel()
    await screen.findByTestId('invitations-empty')

    fireEvent.change(screen.getByTestId('invite-email'), { target: { value: 'alan@example.com' } })
    fireEvent.change(screen.getByTestId('invite-role'), { target: { value: 'admin' } })
    fireEvent.click(screen.getByTestId('invite-submit'))

    expect(vi.mocked(createInvitation)).toHaveBeenCalledWith(
      'sp_7f3a9c',
      'alan@example.com',
      'admin',
    )
  })

  it('does not offer owner to an admin', async () => {
    renderPanel(makeSpace({ my_role: 'admin' }))
    await screen.findByTestId('invitations-empty')

    const select = screen.getByTestId('invite-role') as HTMLSelectElement
    // Mirrors the server's refusal to let anyone invite above themselves.
    // Ergonomics, not enforcement — `create_invitation` re-checks and answers 403.
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['admin', 'member'])
  })

  it('shows a rejected invitation as an error on the form', async () => {
    vi.mocked(createInvitation).mockResolvedValue(
      conflict('That address already belongs to a member of this Space.'),
    )
    renderPanel()
    await screen.findByTestId('invitations-empty')

    fireEvent.change(screen.getByTestId('invite-email'), { target: { value: 'ada@example.com' } })
    fireEvent.click(screen.getByTestId('invite-submit'))

    const error = await screen.findByTestId('invite-error')
    expect(error.textContent).toBe('That address already belongs to a member of this Space.')
  })

  it('revokes a pending invitation and keeps the row as history', async () => {
    vi.mocked(listInvitations).mockResolvedValue(ok([makeInvitation({ id: 20, status: 'pending' })]))
    vi.mocked(revokeInvitation).mockResolvedValue(ok(makeInvitation({ id: 20, status: 'revoked' })))
    renderPanel()

    fireEvent.click(await screen.findByTestId('invitation-revoke-20'))

    expect(vi.mocked(revokeInvitation)).toHaveBeenCalledWith('sp_7f3a9c', 20)
    // Replaced, not removed: who invited whom outlives the access.
    await vi.waitFor(() =>
      expect(screen.getByTestId('invitation-status-20').textContent).toBe('revoked'),
    )
    expect(screen.getByTestId('invitation-20')).toBeTruthy()
    // And once revoked there is nothing left to revoke.
    expect(screen.queryByTestId('invitation-revoke-20')).toBeNull()
  })

  it('offers no revoke on an already-accepted invitation', async () => {
    vi.mocked(listInvitations).mockResolvedValue(
      ok([makeInvitation({ id: 21, status: 'accepted' })]),
    )

    renderPanel()

    await screen.findByTestId('invitation-21')
    // Revoking it would not remove the membership it already became — the
    // server refuses with a 409 that says to remove the membership instead.
    expect(screen.queryByTestId('invitation-revoke-21')).toBeNull()
    expect(screen.getByTestId('invitation-status-21').textContent).toBe('accepted')
  })

  it('keeps a failed revoke next to its own row', async () => {
    vi.mocked(listInvitations).mockResolvedValue(
      ok([makeInvitation({ id: 20 }), makeInvitation({ id: 21, email: 'b@example.com' })]),
    )
    vi.mocked(revokeInvitation).mockResolvedValue(forbidden('You are no longer an admin here.'))
    renderPanel()

    fireEvent.click(await screen.findByTestId('invitation-revoke-20'))

    const error = await screen.findByTestId('invitation-error-20')
    expect(error.textContent).toBe('You are no longer an admin here.')
    expect(screen.queryByTestId('invitation-error-21')).toBeNull()
  })

  it('offers nothing on an archived Space', async () => {
    vi.mocked(listInvitations).mockResolvedValue(ok([makeInvitation({ id: 20 })]))

    renderPanel(makeSpace({ archived_at: '2026-07-20T09:00:00.000Z' }))

    await screen.findByTestId('invitation-20')
    expect(screen.getByTestId('invite-submit').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('invite-email').hasAttribute('disabled')).toBe(true)
    expect(screen.queryByTestId('invitation-revoke-20')).toBeNull()
  })
})
