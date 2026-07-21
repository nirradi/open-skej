import { useCallback, useEffect, useState } from 'react'

import { listMembers, removeMember, updateMemberRole, type Member, type Space } from '../api'
import { assignableRoles, messageFor, ROLE_LABELS } from './messages'

/** What the member list resolved to. `null` while the first load is in flight. */
type Load = { kind: 'members'; members: Member[] } | { kind: 'error'; message: string } | null

/**
 * The member list, with role changes and removal.
 *
 * ## Where the authorization actually is
 *
 * Nowhere in this file. The role select omits options the server would refuse
 * and the whole panel is only rendered for admins, but both are conveniences —
 * anyone can edit the bundle they were served, so the thing that stops a member
 * promoting themselves is `require_space_role` on the backend. What the hiding
 * buys is that an admin is not shown a control that will always fail.
 *
 * That is also why every action here handles `forbidden` and `conflict` rather
 * than assuming the pre-filtering was sufficient. Two admins acting at once is
 * enough to make the UI's picture stale: the other one can demote you between
 * your page load and your click.
 *
 * ## The last-owner refusal
 *
 * Demoting or removing the final owner is refused by the server with a 409 whose
 * body explains what to do instead. That copy is shown verbatim, next to the row
 * that caused it. Before the client modelled `conflict` this surfaced as
 * "Something went wrong on our end", which is both false and useless — the
 * server knew exactly what was wrong and said so.
 */
export function MembersPanel({
  space,
  refreshToken,
  onMembershipChanged,
}: {
  space: Space
  /** Bumped by the access-request queue: approving someone adds a member here. */
  refreshToken: number
  onMembershipChanged: () => void
}) {
  const [load, setLoad] = useState<Load>(null)
  /** Per-row error, keyed by user id, so a refusal appears where it happened. */
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({})
  /** The row with an action in flight, so its buttons can be disabled. */
  const [busyUserId, setBusyUserId] = useState<number | null>(null)

  const archived = space.archived_at !== null

  const refresh = useCallback(async () => {
    const result = await listMembers(space.public_id)
    setLoad(
      result.outcome === 'ok'
        ? { kind: 'members', members: result.data }
        : { kind: 'error', message: messageFor(result) },
    )
  }, [space.public_id])

  useEffect(() => {
    let cancelled = false

    void listMembers(space.public_id).then((result) => {
      if (cancelled) return
      setLoad(
        result.outcome === 'ok'
          ? { kind: 'members', members: result.data }
          : { kind: 'error', message: messageFor(result) },
      )
    })

    return () => {
      cancelled = true
    }
  }, [space.public_id, refreshToken])

  async function handleRoleChange(member: Member, role: Member['role']) {
    setBusyUserId(member.user_id)
    setRowErrors((errors) => ({ ...errors, [member.user_id]: '' }))

    const result = await updateMemberRole(space.public_id, member.user_id, role)
    setBusyUserId(null)

    if (result.outcome === 'ok') {
      await refresh()
      onMembershipChanged()
      return
    }

    setRowErrors((errors) => ({ ...errors, [member.user_id]: messageFor(result) }))
  }

  async function handleRemove(member: Member) {
    setBusyUserId(member.user_id)
    setRowErrors((errors) => ({ ...errors, [member.user_id]: '' }))

    const result = await removeMember(space.public_id, member.user_id)
    setBusyUserId(null)

    if (result.outcome === 'ok') {
      await refresh()
      onMembershipChanged()
      return
    }

    setRowErrors((errors) => ({ ...errors, [member.user_id]: messageFor(result) }))
  }

  if (load === null) {
    return (
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-900">Members</h2>
        <p className="mt-2 text-sm text-slate-600" data-testid="members-loading" role="status">
          Loading members…
        </p>
      </section>
    )
  }

  if (load.kind === 'error') {
    return (
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-900">Members</h2>
        <p className="mt-2 text-sm text-red-700" data-testid="members-error" role="alert">
          {load.message}
        </p>
      </section>
    )
  }

  const roles = assignableRoles(space.my_role)

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4" data-testid="members-panel">
      <h2 className="text-sm font-semibold text-slate-900">Members</h2>

      {load.members.length === 0 ? (
        // Not reachable in practice — a Space always has at least its owner —
        // but a list that renders nothing for an empty array is a list that
        // looks broken the one time it happens.
        <p className="mt-2 text-sm text-slate-600" data-testid="members-empty">
          Nobody is in this Space yet.
        </p>
      ) : (
        <ul className="mt-3 divide-y divide-slate-100">
          {load.members.map((member) => (
            <li key={member.user_id} className="py-3" data-testid={`member-${member.user_id}`}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm text-slate-900">{member.name ?? member.email}</p>
                  {member.name !== null && (
                    <p className="truncate text-xs text-slate-500">{member.email}</p>
                  )}
                </div>

                <div className="flex items-center gap-2">
                  <label className="sr-only" htmlFor={`role-${member.user_id}`}>
                    Role for {member.email}
                  </label>
                  <select
                    id={`role-${member.user_id}`}
                    className="rounded border border-slate-300 px-2 py-1 text-sm"
                    data-testid={`member-role-${member.user_id}`}
                    value={member.role}
                    disabled={archived || busyUserId === member.user_id}
                    onChange={(event) =>
                      void handleRoleChange(member, event.target.value as Member['role'])
                    }
                  >
                    {/* The member's current role is always present even when it
                        is one this admin may not assign — an admin looking at an
                        owner must see "Owner", not a select silently showing the
                        wrong value. */}
                    {(roles as readonly Member['role'][]).includes(member.role) ? null : (
                      <option value={member.role}>{ROLE_LABELS[member.role]}</option>
                    )}
                    {roles.map((role) => (
                      <option key={role} value={role}>
                        {ROLE_LABELS[role]}
                      </option>
                    ))}
                  </select>

                  <button
                    type="button"
                    className="rounded border border-slate-300 px-2 py-1 text-sm text-red-700 disabled:opacity-50"
                    data-testid={`member-remove-${member.user_id}`}
                    disabled={archived || busyUserId === member.user_id}
                    onClick={() => void handleRemove(member)}
                  >
                    Remove
                  </button>
                </div>
              </div>

              {rowErrors[member.user_id] ? (
                <p
                  className="mt-2 text-sm text-red-700"
                  data-testid={`member-error-${member.user_id}`}
                  role="alert"
                >
                  {rowErrors[member.user_id]}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
