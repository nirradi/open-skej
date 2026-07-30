import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { listResources, type Resource, type SpacePreview } from '../api'
import { messageFor } from '../ui/messages'
import { NotFoundCard, PAGE_CLASS, SpaceAccessGate } from './SpaceAccessGate'

/**
 * `/s/{public_id}` — what someone holding a Space's link sees.
 *
 * ## This is the only screen a stranger reaches on a Space link
 *
 * Every other route in the app is for people who are already inside something.
 * This one is the outside of the door: the link *is* the capability, sharing it
 * is the entire distribution model, and the person opening it may have no
 * membership, no account, and no idea what Open-Skej is.
 *
 * The door itself — the mode check, the session check, the `previewSpace`
 * fetch, the sign-in cards, the not-found card, and the access request — lives
 * in `SpaceAccessGate`, shared with `/s/{public_id}/resources/{resource_id}`:
 * admission is Space-level, so a Resource link shows a stranger the identical
 * door. This component supplies only what is specific to landing at the Space
 * URL itself: `returnTo` pointing back at `/s/{public_id}`, and, once the
 * gate confirms the caller is a member, `SpaceMemberView` — the Space's name,
 * description, and a picker onto its Resources — rather than sending them to
 * the generic Space list, which would cost them a click back to the very link
 * they just opened.
 */
export function SpacePage() {
  const { publicId } = useParams<{ publicId: string }>()

  // The route pattern makes this unreachable; TypeScript does not know that, and
  // a crash on the app's only public entry point is not worth the assertion.
  if (!publicId) {
    return <NotFoundCard />
  }

  return (
    <SpaceAccessGate publicId={publicId} returnTo={`/s/${publicId}`}>
      {(preview) => <SpaceMemberView publicId={publicId} preview={preview} />}
    </SpaceAccessGate>
  )
}

type ResourceLoad =
  | { kind: 'ok'; resources: Resource[] }
  | { kind: 'error'; message: string }
  | null

/**
 * What a member sees at `/s/{public_id}`: the Space itself, and a picker onto
 * one of its Resources — the calendars a member may book against.
 *
 * A Space with no Resource is representable in the schema but never produced
 * by the product (creating a Space auto-creates one), so the empty state
 * below is a defensive rendering, not a reachable one.
 */
function SpaceMemberView({ publicId, preview }: { publicId: string; preview: SpacePreview }) {
  const [load, setLoad] = useState<ResourceLoad>(null)

  useEffect(() => {
    let cancelled = false

    void listResources(publicId).then((result) => {
      if (cancelled) return

      if (result.outcome === 'ok') {
        setLoad({ kind: 'ok', resources: result.data })
      } else {
        setLoad({ kind: 'error', message: messageFor(result) })
      }
    })

    return () => {
      cancelled = true
    }
  }, [publicId])

  return (
    <main className={PAGE_CLASS}>
      <div className="w-full max-w-md">
        <h1 className="text-2xl font-semibold text-slate-900" data-testid="space-name">
          {preview.name}
        </h1>
        {preview.description ? (
          <p className="mt-2 text-sm text-slate-600" data-testid="space-description">
            {preview.description}
          </p>
        ) : null}

        <div className="mt-6">
          {load === null && (
            <p className="text-sm text-slate-600" data-testid="resource-list-loading" role="status">
              Loading its Resources…
            </p>
          )}

          {load?.kind === 'error' && (
            <p className="text-sm text-red-700" data-testid="resource-list-error" role="alert">
              {load.message}
            </p>
          )}

          {load?.kind === 'ok' && load.resources.length === 0 && (
            <p className="text-sm text-slate-600" data-testid="resource-list-empty">
              This Space has no Resources yet. Ask an admin to add one.
            </p>
          )}

          {load?.kind === 'ok' && load.resources.length > 0 && (
            <ul className="space-y-2" data-testid="resource-list">
              {load.resources.map((resource) => (
                <li
                  key={resource.id}
                  className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm"
                >
                  <Link
                    to={`/s/${publicId}/resources/${resource.id}`}
                    className="font-medium text-slate-900 hover:underline"
                    data-testid={`resource-list-item-${resource.id}`}
                  >
                    {resource.name}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </main>
  )
}
