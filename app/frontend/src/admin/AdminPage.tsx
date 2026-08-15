import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { listSpaces, type Space } from '../api'
import { AccessRequestsPanel } from './AccessRequestsPanel'
import { ArchiveSpacePanel } from './ArchiveSpacePanel'
import { CreateSpaceForm } from './CreateSpaceForm'
import { InvitationsPanel } from './InvitationsPanel'
import { MembersPanel } from './MembersPanel'
import { messageFor } from './messages'
import { ResourcesPanel } from './ResourcesPanel'
import { ShareLink } from './ShareLink'
import { SpaceSchedulePanel } from './SpaceSchedulePanel'

type Load = { kind: 'spaces'; spaces: Space[] } | { kind: 'error'; message: string } | null

/**
 * The `/admin` dashboard.
 *
 * ## What this screen is, and what it is not
 *
 * It manages **people** — members and their roles, the access-request queue,
 * invitations, and archiving — the Space's **timezone**, the one property of
 * its schedule that stays a plain field rather than a rule instance (see
 * `SpaceSchedulePanel`), and its **Resources** — the bookable calendars a
 * venue holds (`ResourcesPanel`). Every booking constraint — operating hours,
 * slot interval, duration and frequency caps — is a `space_rules` row, edited
 * on its own page at `/s/{public_id}/rules`, linked from here rather than
 * duplicated into this dashboard.
 *
 * ## Resource management lives here, not on the Space page
 *
 * `POST /spaces/{public_id}/resources` is admin+, exactly like every other
 * write this dashboard already makes — creating a Resource is venue
 * management, not booking, and a plain member never sees the control. A
 * second admin surface on `SpacePage` (`/s/{public_id}`) was considered and
 * rejected: that screen is where a *member* picks a Resource to book against,
 * and splitting venue management across two screens would mean an admin has
 * to remember which one holds which control for no gain — everything else an
 * admin configures is already here.
 *
 * The Space picker below is a list of *memberships*, which is a different thing
 * from a directory. `GET /spaces` returns the Spaces the caller belongs to and
 * there is no endpoint that returns any others — a Space you were never let into
 * is not filtered out of the response, it is unreachable without its unguessable
 * link. No "browse all Spaces" view exists to build, by design.
 *
 * ## No Auth0 hook is called here
 *
 * Nothing in `src/admin/` calls `useAuth0()`, and that is deliberate rather than
 * incidental. With Auth0 unconfigured there is no `Auth0Provider` in the tree at
 * all — `AuthProvider` keeps rendering the app so the unauthenticated calendar
 * survives a missing tenant — so calling the hook in that state would crash.
 * `ProtectedRoute` already handles the config check before this component
 * renders, and everything this page needs about the user comes from the API:
 * `my_role` travels on each Space. Keeping the hook out entirely means the guard
 * cannot be defeated by someone later rendering `AdminPage` outside the route.
 *
 * ## Roles are read from the server, and re-checked by it
 *
 * `space.my_role` decides which panels appear. That is a convenience so a member
 * is not shown controls that would only ever fail, and it is never the security
 * boundary: `require_space_role` re-checks every one of these calls, and each
 * panel handles the `forbidden` outcome rather than assuming the hiding worked.
 */
export function AdminPage() {
  const [load, setLoad] = useState<Load>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  /** Bumped when a membership changes, so the members list refetches. */
  const [membershipToken, setMembershipToken] = useState(0)

  useEffect(() => {
    let cancelled = false

    void listSpaces({ includeArchived: true }).then((result) => {
      if (cancelled) return

      if (result.outcome !== 'ok') {
        setLoad({ kind: 'error', message: messageFor(result) })
        return
      }

      setLoad({ kind: 'spaces', spaces: result.data })
      setSelectedId((current) => current ?? result.data[0]?.public_id ?? null)
    })

    return () => {
      cancelled = true
    }
  }, [])

  const handleCreated = useCallback((space: Space) => {
    setLoad((current) =>
      current?.kind === 'spaces'
        ? { kind: 'spaces', spaces: [space, ...current.spaces] }
        : { kind: 'spaces', spaces: [space] },
    )
    setSelectedId(space.public_id)
  }, [])

  /** Replaces one Space in the list — used when archiving changes its state. */
  const handleSpaceChanged = useCallback((updated: Space) => {
    setLoad((current) =>
      current?.kind === 'spaces'
        ? {
            kind: 'spaces',
            spaces: current.spaces.map((space) =>
              space.public_id === updated.public_id ? updated : space,
            ),
          }
        : current,
    )
  }, [])

  const handleMembershipChanged = useCallback(() => {
    setMembershipToken((token) => token + 1)
  }, [])

  const spaces = load?.kind === 'spaces' ? load.spaces : []
  const selected = spaces.find((space) => space.public_id === selectedId) ?? null

  return (
    <main className="min-h-screen bg-slate-50 p-8 text-slate-800">
      <div className="mx-auto max-w-3xl">
        <h1 className="text-2xl font-semibold text-slate-900">Manage Spaces</h1>
        <p className="mt-2 mb-6 text-sm text-slate-600">
          Create a Space, decide who is in it, and hand out its link.
        </p>

        <div className="space-y-6">
          <CreateSpaceForm onCreated={handleCreated} />

          {load === null ? (
            <p className="text-sm text-slate-600" data-testid="spaces-loading" role="status">
              Loading your Spaces…
            </p>
          ) : load.kind === 'error' ? (
            <p className="text-sm text-red-700" data-testid="spaces-error" role="alert">
              {load.message}
            </p>
          ) : spaces.length === 0 ? (
            <p className="text-sm text-slate-600" data-testid="spaces-empty">
              You are not in any Spaces yet. Create one above, or open a Space link someone shared
              with you.
            </p>
          ) : (
            <>
              <section
                className="rounded-lg border border-slate-200 bg-white p-4"
                data-testid="space-picker-panel"
              >
                <label className="block text-xs text-slate-600" htmlFor="space-picker">
                  Space
                </label>
                <select
                  id="space-picker"
                  className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
                  data-testid="space-picker"
                  value={selectedId ?? ''}
                  onChange={(event) => setSelectedId(event.target.value)}
                >
                  {spaces.map((space) => (
                    <option key={space.public_id} value={space.public_id}>
                      {space.name}
                      {space.archived_at !== null ? ' (archived)' : ''}
                    </option>
                  ))}
                </select>

                {selected ? <ShareLink publicId={selected.public_id} /> : null}
              </section>

              {selected ? (
                <SpaceAdmin
                  space={selected}
                  membershipToken={membershipToken}
                  onMembershipChanged={handleMembershipChanged}
                  onSpaceChanged={handleSpaceChanged}
                />
              ) : null}
            </>
          )}
        </div>
      </div>
    </main>
  )
}

/**
 * The panels for one Space, chosen by the caller's role in it.
 *
 * A plain member sees the notice and nothing else — no member list, no queue, no
 * invitations. The invitation list in particular is admin-only on the server too,
 * because it names people who are *not* in the Space: who is being recruited is
 * not every member's business.
 */
function SpaceAdmin({
  space,
  membershipToken,
  onMembershipChanged,
  onSpaceChanged,
}: {
  space: Space
  membershipToken: number
  onMembershipChanged: () => void
  onSpaceChanged: (space: Space) => void
}) {
  const isAdmin = space.my_role === 'admin' || space.my_role === 'owner'
  const archived = space.archived_at !== null

  if (!isAdmin) {
    return (
      <section
        className="rounded-lg border border-slate-200 bg-white p-4"
        data-testid="member-notice"
      >
        <h2 className="text-sm font-semibold text-slate-900">{space.name}</h2>
        <p className="mt-2 text-sm text-slate-600">
          You are a member of this Space. Only its admins can manage members and invitations.
        </p>
      </section>
    )
  }

  return (
    <div className="space-y-6" data-testid="space-admin">
      {archived ? (
        <p
          className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
          data-testid="archived-banner"
          role="status"
        >
          This Space is archived. Nothing in it can be changed.
        </p>
      ) : null}

      <AccessRequestsPanel space={space} onApproved={onMembershipChanged} />
      <MembersPanel
        space={space}
        refreshToken={membershipToken}
        onMembershipChanged={onMembershipChanged}
      />
      <InvitationsPanel space={space} />
      <SpaceSchedulePanel space={space} onSpaceChanged={onSpaceChanged} />
      {/* Nothing else on this page reads the Resource list today, so `onChanged`
          is a no-op — it exists so a future caller (a Resources count, a
          single-Resource redirect check) is a wiring change, not a rewrite. */}
      <ResourcesPanel space={space} onChanged={() => {}} />

      <section
        className="rounded-lg border border-slate-200 bg-white p-4"
        data-testid="rules-link-panel"
      >
        <h2 className="text-sm font-semibold text-slate-900">Rules</h2>
        <p className="mt-1 text-xs text-slate-500">
          Operating hours, slot interval, duration and frequency caps — every booking constraint
          this Space enforces.
        </p>
        <Link
          to={`/s/${space.public_id}/rules`}
          className="mt-3 inline-block rounded bg-slate-800 px-3 py-1 text-sm text-white"
          data-testid="space-rules-link"
        >
          Manage rules
        </Link>
      </section>

      {/* Archiving is owner-only on the server, so an admin is not offered it. */}
      {space.my_role === 'owner' ? (
        <ArchiveSpacePanel space={space} onArchived={onSpaceChanged} />
      ) : null}
    </div>
  )
}
