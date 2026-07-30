import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'

import { previewSpace, requestAccess, type SpacePreview } from '../api'
import { LoginControls, MissingConfigNotice, useAuthMode, useSession } from '../auth'
import { messageFor } from '../ui/messages'

/**
 * Copy for a Space that did not resolve.
 *
 * **Read `ApiNotFound` in `src/api/types.ts` before editing this string.** A 404
 * from a Space route means "no such Space, *or* not yours", and the backend
 * spends a 404 rather than a 403 precisely so that an outsider cannot confirm an
 * unguessable id exists. Any wording that implies the Space is real — "you don't
 * have access to this Space", "ask an admin to let you in" — hands back the fact
 * the status code was spent to hide, and turns every forwarded link into an
 * oracle for whether it is still live. It is also wrong half the time.
 *
 * So the sentence talks about the *link*, which is the one thing we can honestly
 * say is in the user's hands, and never about the Space.
 */
const NOT_FOUND_HEADING = "That link doesn't work"
const NOT_FOUND_BODY =
  'Check that you copied the whole link, including the part after the last slash. ' +
  'If it was shared with you a while ago, ask whoever sent it for a current one.'

export const CARD_CLASS = 'w-full max-w-md rounded-lg border border-slate-200 bg-white p-6 shadow-sm'
export const PAGE_CLASS = 'flex min-h-screen items-center justify-center bg-slate-50 p-8'
const BUTTON_CLASS =
  'rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white transition ' +
  'hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-400 ' +
  'focus:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50'

/**
 * The door onto a Space, shared by every route that a stranger's link can
 * name — `/s/{public_id}` and `/s/{public_id}/resources/{resource_id}` alike.
 * Admission is Space-level (`.claude/rules/identity-and-access.md`), so a
 * Resource id in the URL changes nothing about who gets let in or what they
 * see on the way: the Resource is not a separate capability, and a stranger
 * holding a Resource link gets the identical Space preview and access
 * request a Space link would have shown them.
 *
 * It owns, in order: the auth-mode check (`unconfigured` →
 * `MissingConfigNotice`), the session status (`loading` / `unauthenticated`
 * → the sign-in card), the `previewSpace` fetch (`not_found` →
 * `NotFoundCard`, `signed_out` → the sign-in card, `error` → the error
 * card), and non-member status (the preview and the access request). Only
 * once the caller is confirmed a member does it render `children`, which is
 * the calling route's own screen — `SpaceMemberView`'s Resource picker for
 * `/s/{public_id}`, the calendar for the Resource route.
 *
 * **`returnTo` is required, not defaulted off `window.location`.** Stating it
 * explicitly at each call site means a later change to how a route is
 * mounted cannot silently start returning people to the wrong place — see
 * `LoginControls` for the default this deliberately does not fall back to.
 * The Resource route passes its own full path so that approval, and any
 * later re-visit of the sign-in card, lands back on the Resource URL rather
 * than the Space's.
 *
 * **The mode check is an outer component and the session read is an inner
 * one**, because a hook cannot be called conditionally: with neither Auth0
 * nor sandbox mode configured there is no session provider in the tree at
 * all, so `useSession()` must never run in that state.
 */
export function SpaceAccessGate({
  publicId,
  returnTo,
  children,
}: {
  publicId: string
  returnTo: string
  children: (preview: SpacePreview) => ReactNode
}) {
  const mode = useAuthMode()

  if (mode.kind === 'unconfigured') {
    return <MissingConfigNotice missing={mode.missing} />
  }

  return (
    <SpaceSessionGate publicId={publicId} returnTo={returnTo}>
      {children}
    </SpaceSessionGate>
  )
}

/** Requires a session before anything is fetched. Only rendered once some session provider is mounted. */
function SpaceSessionGate({
  publicId,
  returnTo,
  children,
}: {
  publicId: string
  returnTo: string
  children: (preview: SpacePreview) => ReactNode
}) {
  const { status } = useSession()

  // The state that gets forgotten: the SDK starts every page load
  // unauthenticated while it looks for an existing session, so treating
  // "not authenticated" as "signed out" flashes the sign-in card at a member
  // before swapping it for their Space.
  if (status === 'loading') {
    return (
      <main className={PAGE_CLASS}>
        <p className="text-sm text-slate-600" data-testid="space-auth-loading" role="status">
          Checking your session…
        </p>
      </main>
    )
  }

  if (status === 'unauthenticated') {
    return (
      <SignInCard
        returnTo={returnTo}
        heading="You’ve been sent a Space"
        body="Sign in to see what this link opens. If you’re not a member yet, you’ll be able to ask for access on the next screen."
      />
    )
  }

  return (
    <SpacePreviewGate publicId={publicId} returnTo={returnTo}>
      {children}
    </SpacePreviewGate>
  )
}

/**
 * The sign-in prompt this gate shows wherever it needs one — a cold visitor
 * in `SpaceSessionGate` above, and a member whose session turned out to be
 * stale in `SpacePreviewGate` below. One component instead of two
 * near-identical cards, so the `returnTo` and the markup cannot drift apart
 * between them.
 */
function SignInCard({ returnTo, heading, body }: { returnTo: string; heading: string; body: string }) {
  return (
    <main className={PAGE_CLASS}>
      <div className={CARD_CLASS} data-testid="space-sign-in">
        <h1 className="text-lg font-semibold text-slate-900">{heading}</h1>
        <p className="mt-2 mb-4 text-sm text-slate-600">{body}</p>
        <LoginControls returnTo={returnTo} />
      </div>
    </main>
  )
}

type Load =
  | { kind: 'preview'; preview: SpacePreview }
  | { kind: 'not_found' }
  | { kind: 'signed_out' }
  | { kind: 'error'; message: string }
  | null

/** The preview itself, and the action it offers. Signed in by construction. */
function SpacePreviewGate({
  publicId,
  returnTo,
  children,
}: {
  publicId: string
  returnTo: string
  children: (preview: SpacePreview) => ReactNode
}) {
  const [load, setLoad] = useState<Load>(null)

  const fetchPreview = useCallback(() => {
    let cancelled = false

    void previewSpace(publicId).then((result) => {
      if (cancelled) return

      if (result.outcome === 'ok') {
        setLoad({ kind: 'preview', preview: result.data })
      } else if (result.outcome === 'not_found') {
        // Its own branch rather than a message, because this is the one outcome
        // whose copy is a security question — see `NOT_FOUND_HEADING`.
        setLoad({ kind: 'not_found' })
      } else if (result.outcome === 'unauthenticated') {
        // The SDK said we were signed in and the server disagreed — a session
        // that lapsed between the two, or a token refresh that failed silently.
        // The remedy is signing in again, so this must not render as a generic
        // error with no way forward: a dead end on a public entry point is how
        // a share link stops working for no visible reason.
        setLoad({ kind: 'signed_out' })
      } else {
        setLoad({ kind: 'error', message: messageFor(result) })
      }
    })

    return () => {
      cancelled = true
    }
  }, [publicId])

  useEffect(fetchPreview, [fetchPreview])

  /** Called when a request is filed: re-reads the status rather than guessing it. */
  const handleRequested = useCallback(() => {
    setLoad((current) =>
      current?.kind === 'preview'
        ? { kind: 'preview', preview: { ...current.preview, status: 'pending' } }
        : current,
    )
  }, [])

  if (load === null) {
    return (
      <main className={PAGE_CLASS}>
        <p className="text-sm text-slate-600" data-testid="space-loading" role="status">
          Opening this Space…
        </p>
      </main>
    )
  }

  if (load.kind === 'not_found') {
    return <NotFoundCard />
  }

  if (load.kind === 'signed_out') {
    return (
      <SignInCard returnTo={returnTo} heading="Sign in to open this Space" body="You’re signed out." />
    )
  }

  if (load.kind === 'error') {
    return (
      <main className={PAGE_CLASS}>
        <div className={CARD_CLASS}>
          <p className="text-sm text-red-700" data-testid="space-error" role="alert">
            {load.message}
          </p>
        </div>
      </main>
    )
  }

  const { preview } = load

  // A member is already inside; the preview is the outside of the door and has
  // nothing to tell them. `children` is the route's own screen from here.
  if (preview.status === 'member') {
    return <>{children(preview)}</>
  }

  return (
    <main className={PAGE_CLASS}>
      <div className={CARD_CLASS} data-testid="space-preview">
        <h1 className="text-lg font-semibold text-slate-900" data-testid="space-name">
          {preview.name}
        </h1>
        {preview.description ? (
          <p className="mt-2 text-sm text-slate-600" data-testid="space-description">
            {preview.description}
          </p>
        ) : null}

        <div className="mt-4 border-t border-slate-200 pt-4">
          <StatusSection publicId={publicId} status={preview.status} onRequested={handleRequested} />
        </div>
      </div>
    </main>
  )
}

/**
 * The three states a non-member can be in, and what each one offers.
 *
 * `member` never reaches here — `SpacePreviewGate` takes over a level up — so
 * this switch covers the states in which the user is still outside.
 */
function StatusSection({
  publicId,
  status,
  onRequested,
}: {
  publicId: string
  status: SpacePreview['status']
  onRequested: () => void
}) {
  if (status === 'pending') {
    return (
      <div data-testid="space-status-pending">
        <p className="text-sm text-slate-700">
          Your request is with this Space&rsquo;s admins. You&rsquo;ll be able to book once one of
          them approves it — nothing else to do here.
        </p>
      </div>
    )
  }

  if (status === 'denied') {
    // Asking again is genuinely allowed: only *pending* requests are unique per
    // user, and a denied row is kept as history rather than as a permanent bar.
    // Offering the form again is the honest reading of that schema, not a
    // loophole — the same admin still decides.
    return (
      <div data-testid="space-status-denied">
        <p className="mb-4 text-sm text-slate-700">
          Your last request wasn&rsquo;t approved. You can ask again if something has changed.
        </p>
        <RequestAccessForm publicId={publicId} onRequested={onRequested} label="Ask again" />
      </div>
    )
  }

  return (
    <div data-testid="space-status-none">
      <p className="mb-4 text-sm text-slate-700">
        You&rsquo;re not in this Space yet. Ask its admins for access — you can add a note saying who
        you are.
      </p>
      <RequestAccessForm publicId={publicId} onRequested={onRequested} label="Request access" />
    </div>
  )
}

type Submission = { kind: 'idle' } | { kind: 'sending' } | { kind: 'error'; message: string }

/**
 * The ask.
 *
 * The message is optional and free text, matching the server: the admin deciding
 * has otherwise only an email address to go on, and an email address is not much
 * on which to let a stranger into a private Space.
 *
 * A failure keeps the typed note on screen. Losing someone's explanation to a
 * dropped connection would mean retyping it, and the note is the whole reason
 * this form is more than a button.
 */
function RequestAccessForm({
  publicId,
  onRequested,
  label,
}: {
  publicId: string
  onRequested: () => void
  label: string
}) {
  const [message, setMessage] = useState('')
  const [submission, setSubmission] = useState<Submission>({ kind: 'idle' })

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setSubmission({ kind: 'sending' })

    const trimmed = message.trim()
    const result = await requestAccess(publicId, trimmed === '' ? null : trimmed)

    if (result.outcome === 'ok') {
      onRequested()
      return
    }

    // Includes `conflict`, whose sentence is the server's own and the only
    // statement of *which* refusal this is — archived, already a member, or a
    // request already pending. Showing it verbatim beats inventing copy for a
    // distinction we would have to parse English to recover.
    setSubmission({ kind: 'error', message: messageFor(result) })
  }

  return (
    <form onSubmit={handleSubmit} data-testid="request-access-form">
      <label className="block text-xs text-slate-600" htmlFor="access-message">
        Note for the admins (optional)
      </label>
      <textarea
        id="access-message"
        className="mt-1 w-full rounded border border-slate-300 px-2 py-1 text-sm"
        data-testid="access-message"
        rows={3}
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        placeholder="I'm on the Tuesday team"
      />

      <button
        type="submit"
        className={`${BUTTON_CLASS} mt-3`}
        data-testid="request-access"
        disabled={submission.kind === 'sending'}
      >
        {submission.kind === 'sending' ? 'Sending…' : label}
      </button>

      {submission.kind === 'error' ? (
        <p className="mt-3 text-sm text-red-700" data-testid="request-access-error" role="alert">
          {submission.message}
        </p>
      ) : null}
    </form>
  )
}

/** The dead-link card. Says nothing about whether a Space is behind the id. */
export function NotFoundCard() {
  return (
    <main className={PAGE_CLASS}>
      <div className={CARD_CLASS} data-testid="space-not-found">
        <h1 className="text-lg font-semibold text-slate-900">{NOT_FOUND_HEADING}</h1>
        <p className="mt-2 text-sm text-slate-600">{NOT_FOUND_BODY}</p>
      </div>
    </main>
  )
}
