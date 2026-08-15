import { Link, useParams } from 'react-router-dom'

import { CARD_CLASS, PAGE_CLASS } from './space/SpaceAccessGate'

const LINK_CLASS = 'text-sm font-medium text-slate-700 underline hover:text-slate-900'

/**
 * The catch-all for an address that names no route at all —
 * `App.tsx`'s `path="*"`, and `path="/s/:publicId/*"` for the Space-scoped
 * variant just below.
 *
 * ## A different claim than `NotFoundCard`
 *
 * `space/SpaceAccessGate.tsx`'s `NotFoundCard` says "no such Space, or you
 * can't see it" — a statement about a *Space*, deliberately ambiguous
 * because it conceals a membership check
 * (`.claude/rules/identity-and-access.md`, "Spaces are not discoverable").
 * This component says "no such page", which conceals nothing: a caller who
 * typed `/s/{public_id}/members` is not asking about a Space that may or
 * may not exist, they are asking about a screen this application has never
 * had. Reusing `NotFoundCard`'s copy here would quietly widen its
 * deliberately-ambiguous wording to cover a claim that owes nobody any
 * ambiguity, so this gets its own heading, its own body, and its own
 * `data-testid` instead.
 *
 * It shares `NotFoundCard`'s chrome (`PAGE_CLASS` / `CARD_CLASS`) because
 * the two are the same *kind* of screen — a centered card standing in for
 * one that could not be shown — not because they mean the same thing.
 *
 * ## Not authenticated
 *
 * Deliberately not wrapped in `ProtectedRoute` and it reads no session: a
 * wrong address is wrong whether or not the visitor is signed in, and
 * `ProtectedRoute`'s "you need an account" notice would misdiagnose a typo
 * as a login problem.
 *
 * ## `publicId` is what a Space-scoped miss can say that a bare one can't
 *
 * A top-level miss (`/nonsense`) can only point home. A miss under
 * `/s/{public_id}/...` still names a real Space the visitor was looking
 * at, so it also offers the way back to it and to the console — the two
 * places task 9.1 already made reachable from a Space. It says nothing
 * about whether that Space exists or the visitor can see it; following
 * either link goes through the ordinary access gate exactly as if the
 * visitor had typed it themselves, so no check is skipped by offering it.
 */
export function NotFoundPage({ publicId }: { publicId?: string }) {
  return (
    <main className={PAGE_CLASS}>
      <div className={CARD_CLASS} data-testid="page-not-found">
        <h1 className="text-lg font-semibold text-slate-900">That page doesn&apos;t exist</h1>
        <p className="mt-2 text-sm text-slate-600">
          There is nothing at this address. Check what you typed, or use one of the links below.
        </p>
        <nav className="mt-4 flex flex-col items-start gap-2">
          <Link className={LINK_CLASS} to="/">
            Go to your Spaces
          </Link>
          {publicId ? (
            <>
              <Link className={LINK_CLASS} to={`/s/${publicId}`}>
                Back to this Space
              </Link>
              <Link className={LINK_CLASS} to="/admin">
                Open the console
              </Link>
            </>
          ) : null}
        </nav>
      </div>
    </main>
  )
}

/**
 * `App.tsx`'s `path="/s/:publicId/*"` element. Exists only to read
 * `publicId` off the URL — a `Route`'s `element` cannot call a hook itself
 * — and hand it to `NotFoundPage`, the same split `ResourceCalendarRoute`
 * and `SpacePage` already use for their own params.
 */
export function SpaceNotFoundRoute() {
  const { publicId } = useParams<{ publicId: string }>()
  return <NotFoundPage publicId={publicId} />
}
