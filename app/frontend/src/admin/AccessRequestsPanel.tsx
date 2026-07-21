import { useEffect, useState } from 'react'

import {
  approveAccessRequest,
  denyAccessRequest,
  listAccessRequests,
  type AccessRequest,
  type Space,
} from '../api'
import { messageFor } from './messages'

type Load = { kind: 'requests'; requests: AccessRequest[] } | { kind: 'error'; message: string } | null

/**
 * The pending access-request queue, with approve and deny.
 *
 * ## There is deliberately no role selector
 *
 * Approving grants `member`, always. That is a settled product decision, not a
 * placeholder: an admin who wants the new arrival higher up promotes them in the
 * members panel afterwards. The reason to keep it that way is that the members
 * route is where the owner-authority and last-owner invariants are enforced, and
 * a role picker here would need its own copy of both — a second path through the
 * same authorization logic, which is the shape most privilege-escalation bugs
 * arrive in. One extra click beats two enforcement points.
 *
 * ## Only pending requests are shown
 *
 * The server keeps decided rows as history and a denial is not a permanent bar,
 * so the queue asks for `status=pending` rather than filtering client-side.
 * Fetching everything and hiding most of it would put the names of everyone ever
 * refused into a response the screen never uses.
 */
export function AccessRequestsPanel({
  space,
  onApproved,
}: {
  space: Space
  /** Approving creates a membership, so the members panel is now stale. */
  onApproved: () => void
}) {
  const [load, setLoad] = useState<Load>(null)
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({})
  const [busyRequestId, setBusyRequestId] = useState<number | null>(null)

  const archived = space.archived_at !== null

  useEffect(() => {
    let cancelled = false

    void listAccessRequests(space.public_id, { status: 'pending' }).then((result) => {
      if (cancelled) return
      setLoad(
        result.outcome === 'ok'
          ? { kind: 'requests', requests: result.data }
          : { kind: 'error', message: messageFor(result) },
      )
    })

    return () => {
      cancelled = true
    }
  }, [space.public_id])

  async function decide(request: AccessRequest, approve: boolean) {
    setBusyRequestId(request.id)
    setRowErrors((errors) => ({ ...errors, [request.id]: '' }))

    const result = approve
      ? await approveAccessRequest(space.public_id, request.id)
      : await denyAccessRequest(space.public_id, request.id)

    setBusyRequestId(null)

    if (result.outcome === 'ok') {
      // Drop the row locally rather than refetching: the decision is final, the
      // server has already told us it landed, and a refetch would blank the
      // whole queue for a moment over a row we can remove exactly.
      setLoad((current) =>
        current?.kind === 'requests'
          ? { kind: 'requests', requests: current.requests.filter((r) => r.id !== request.id) }
          : current,
      )
      if (approve) onApproved()
      return
    }

    setRowErrors((errors) => ({ ...errors, [request.id]: messageFor(result) }))
  }

  if (load === null) {
    return (
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-900">Access requests</h2>
        <p className="mt-2 text-sm text-slate-600" data-testid="requests-loading" role="status">
          Loading requests…
        </p>
      </section>
    )
  }

  if (load.kind === 'error') {
    return (
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-900">Access requests</h2>
        <p className="mt-2 text-sm text-red-700" data-testid="requests-error" role="alert">
          {load.message}
        </p>
      </section>
    )
  }

  return (
    <section
      className="rounded-lg border border-slate-200 bg-white p-4"
      data-testid="requests-panel"
    >
      <h2 className="text-sm font-semibold text-slate-900">Access requests</h2>

      {load.requests.length === 0 ? (
        <p className="mt-2 text-sm text-slate-600" data-testid="requests-empty">
          No one is waiting to join.
        </p>
      ) : (
        <ul className="mt-3 divide-y divide-slate-100">
          {load.requests.map((request) => (
            <li key={request.id} className="py-3" data-testid={`request-${request.id}`}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm text-slate-900">{request.name ?? request.email}</p>
                  {request.name !== null && (
                    <p className="truncate text-xs text-slate-500">{request.email}</p>
                  )}
                  {request.message !== null && (
                    <p
                      className="mt-1 text-sm text-slate-600 italic"
                      data-testid={`request-message-${request.id}`}
                    >
                      “{request.message}”
                    </p>
                  )}
                </div>

                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="rounded bg-slate-900 px-3 py-1 text-sm text-white disabled:opacity-50"
                    data-testid={`request-approve-${request.id}`}
                    disabled={archived || busyRequestId === request.id}
                    onClick={() => void decide(request, true)}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    className="rounded border border-slate-300 px-3 py-1 text-sm disabled:opacity-50"
                    data-testid={`request-deny-${request.id}`}
                    disabled={archived || busyRequestId === request.id}
                    onClick={() => void decide(request, false)}
                  >
                    Deny
                  </button>
                </div>
              </div>

              {rowErrors[request.id] ? (
                <p
                  className="mt-2 text-sm text-red-700"
                  data-testid={`request-error-${request.id}`}
                  role="alert"
                >
                  {rowErrors[request.id]}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
