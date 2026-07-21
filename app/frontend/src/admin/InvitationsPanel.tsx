import { useEffect, useState } from 'react'

import {
  createInvitation,
  listInvitations,
  revokeInvitation,
  type Invitation,
  type MembershipRole,
  type Space,
} from '../api'
import { assignableRoles, messageFor, ROLE_LABELS } from './messages'

type Load =
  | { kind: 'invitations'; invitations: Invitation[] }
  | { kind: 'error'; message: string }
  | null

/**
 * Invitations: send one at a role, and revoke one that has not been claimed.
 *
 * ## Nothing is emailed
 *
 * The invitation records that an address is pre-approved; the admin still shares
 * the Space link themselves. The invitee is admitted on first login, and only if
 * their token says the address is verified — an invitation trusts the *proof* of
 * an address, never the address as typed here. So the copy below says the link
 * still has to be sent, because an admin who assumes an email went out will sit
 * waiting for someone who was never told.
 *
 * ## The role select stops at the admin's own role
 *
 * `assignableRoles` omits `owner` for an admin, mirroring the server's refusal
 * to let anyone invite above themselves. As everywhere else in this dashboard
 * that is ergonomics, not enforcement: `create_invitation` re-checks it and
 * answers 403, and the submit handler below renders that 403 like any other
 * refusal rather than treating it as impossible.
 *
 * ## Only pending invitations can be revoked
 *
 * An accepted invitation has already become a membership, and revoking it would
 * not take that membership away — the server refuses with a 409 saying so. The
 * button is therefore only rendered for pending rows, and the resolved ones stay
 * visible as history with their status shown.
 */
export function InvitationsPanel({ space }: { space: Space }) {
  const [load, setLoad] = useState<Load>(null)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<MembershipRole>('member')
  const [formError, setFormError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({})
  const [busyInvitationId, setBusyInvitationId] = useState<number | null>(null)

  const archived = space.archived_at !== null

  useEffect(() => {
    let cancelled = false

    void listInvitations(space.public_id).then((result) => {
      if (cancelled) return
      setLoad(
        result.outcome === 'ok'
          ? { kind: 'invitations', invitations: result.data }
          : { kind: 'error', message: messageFor(result) },
      )
    })

    return () => {
      cancelled = true
    }
  }, [space.public_id])

  async function handleInvite(event: React.FormEvent) {
    event.preventDefault()

    const address = email.trim()
    if (address === '') {
      setFormError('Enter an email address to invite.')
      return
    }

    setSubmitting(true)
    setFormError('')

    const result = await createInvitation(space.public_id, address, role)
    setSubmitting(false)

    if (result.outcome === 'ok') {
      const created = result.data
      setLoad((current) =>
        current?.kind === 'invitations'
          ? { kind: 'invitations', invitations: [created, ...current.invitations] }
          : current,
      )
      setEmail('')
      return
    }

    setFormError(messageFor(result))
  }

  async function handleRevoke(invitation: Invitation) {
    setBusyInvitationId(invitation.id)
    setRowErrors((errors) => ({ ...errors, [invitation.id]: '' }))

    const result = await revokeInvitation(space.public_id, invitation.id)
    setBusyInvitationId(null)

    if (result.outcome === 'ok') {
      // The server returns the revoked row rather than 204, and its `status` is
      // the evidence the revocation took effect — so the row is replaced, not
      // removed. The record of who invited whom outlives the access.
      const revoked = result.data
      setLoad((current) =>
        current?.kind === 'invitations'
          ? {
              kind: 'invitations',
              invitations: current.invitations.map((existing) =>
                existing.id === revoked.id ? revoked : existing,
              ),
            }
          : current,
      )
      return
    }

    setRowErrors((errors) => ({ ...errors, [invitation.id]: messageFor(result) }))
  }

  const roles = assignableRoles(space.my_role)

  return (
    <section
      className="rounded-lg border border-slate-200 bg-white p-4"
      data-testid="invitations-panel"
    >
      <h2 className="text-sm font-semibold text-slate-900">Invitations</h2>
      <p className="mt-1 text-xs text-slate-500">
        Inviting pre-approves an address. No email is sent — share the Space link yourself.
      </p>

      <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={(e) => void handleInvite(e)}>
        <div className="min-w-0 flex-1">
          <label className="block text-xs text-slate-600" htmlFor="invite-email">
            Email address
          </label>
          <input
            id="invite-email"
            type="email"
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="invite-email"
            value={email}
            disabled={archived || submitting}
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>

        <div>
          <label className="block text-xs text-slate-600" htmlFor="invite-role">
            Role
          </label>
          <select
            id="invite-role"
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
            data-testid="invite-role"
            value={role}
            disabled={archived || submitting}
            onChange={(event) => setRole(event.target.value as MembershipRole)}
          >
            {roles.map((assignable) => (
              <option key={assignable} value={assignable}>
                {ROLE_LABELS[assignable]}
              </option>
            ))}
          </select>
        </div>

        <button
          type="submit"
          className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white disabled:opacity-50"
          data-testid="invite-submit"
          disabled={archived || submitting}
        >
          {submitting ? 'Inviting…' : 'Invite'}
        </button>
      </form>

      {formError ? (
        <p className="mt-2 text-sm text-red-700" data-testid="invite-error" role="alert">
          {formError}
        </p>
      ) : null}

      {load === null ? (
        <p className="mt-3 text-sm text-slate-600" data-testid="invitations-loading" role="status">
          Loading invitations…
        </p>
      ) : load.kind === 'error' ? (
        <p className="mt-3 text-sm text-red-700" data-testid="invitations-error" role="alert">
          {load.message}
        </p>
      ) : load.invitations.length === 0 ? (
        <p className="mt-3 text-sm text-slate-600" data-testid="invitations-empty">
          Nobody has been invited yet.
        </p>
      ) : (
        <ul className="mt-3 divide-y divide-slate-100">
          {load.invitations.map((invitation) => (
            <li key={invitation.id} className="py-2" data-testid={`invitation-${invitation.id}`}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm text-slate-900">{invitation.email}</p>
                  <p className="text-xs text-slate-500">
                    {ROLE_LABELS[invitation.role]} ·{' '}
                    <span data-testid={`invitation-status-${invitation.id}`}>
                      {invitation.status}
                    </span>
                  </p>
                </div>

                {invitation.status === 'pending' && !archived ? (
                  <button
                    type="button"
                    className="rounded border border-slate-300 px-2 py-1 text-sm text-red-700 disabled:opacity-50"
                    data-testid={`invitation-revoke-${invitation.id}`}
                    disabled={busyInvitationId === invitation.id}
                    onClick={() => void handleRevoke(invitation)}
                  >
                    Revoke
                  </button>
                ) : null}
              </div>

              {rowErrors[invitation.id] ? (
                <p
                  className="mt-1 text-sm text-red-700"
                  data-testid={`invitation-error-${invitation.id}`}
                  role="alert"
                >
                  {rowErrors[invitation.id]}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
