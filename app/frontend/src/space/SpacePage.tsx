import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useAuth0 } from '@auth0/auth0-react'
import { Navigate, useParams } from 'react-router-dom'

import { previewSpace, requestAccess, type SpacePreview } from '../api'
import { LoginControls, MissingConfigNotice, useAuthConfig } from '../auth'
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

const CARD_CLASS = 'w-full max-w-md rounded-lg border border-slate-200 bg-white p-6 shadow-sm'
const PAGE_CLASS = 'flex min-h-screen items-center justify-center bg-slate-50 p-8'
const BUTTON_CLASS =
  'rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white transition ' +
  'hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-400 ' +
  'focus:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50'

/**
 * `/s/{public_id}` — what someone holding a Space's link sees.
 *
 * ## This is the only screen a stranger reaches
 *
 * Every other route in the app is for people who are already inside something.
 * This one is the outside of the door: the link *is* the capability, sharing it
 * is the entire distribution model, and the person opening it may have no
 * membership, no account, and no idea what Open-Skej is. Three things follow.
 *
 * **It is not behind `ProtectedRoute`,** so it does its own config check. With
 * `VITE_AUTH0_*` unset there is no `Auth0Provider` in the tree at all —
 * `AuthProvider` reports a missing configuration rather than enforcing it, so
 * that Stream 1's unauthenticated calendar survives an unconfigured tenant — and
 * `useAuth0()` must therefore not be called in that state. A hook cannot be
 * called conditionally, so the check lives in this component and the hook in
 * `SpaceLinkGate` below. Same split as `ProtectedRoute`, for the same reason.
 *
 * **A signed-out visitor is shown a sign-in card, not the Space.**
 * `GET /preview` sits behind `get_current_user`, because the status it reports —
 * pending, denied, member — is a fact about *you*, and there is no "you" to
 * report on for an anonymous caller. So not even the Space's name can be shown
 * before login. That is a real product consequence and the card says so plainly
 * instead of rendering an empty shell or letting a 401 surface as an error.
 *
 * **Login returns here.** `LoginControls` threads `returnTo` through Auth0's
 * `appState`, and without it a visitor who followed a share link would be
 * deposited on the calendar after signing in, having lost the only handle to the
 * Space that exists. That is why this route renders login in place rather than
 * redirecting to it: the URL stays intact and remains something to come back to.
 */
export function SpacePage() {
  const { publicId } = useParams<{ publicId: string }>()
  const config = useAuthConfig()

  if (config.status === 'missing') {
    return <MissingConfigNotice missing={config.missing} />
  }

  // The route pattern makes this unreachable; TypeScript does not know that, and
  // a crash on the app's only public entry point is not worth the assertion.
  if (!publicId) {
    return <NotFoundCard />
  }

  return <SpaceLinkGate publicId={publicId} />
}

/** Requires a session before anything is fetched. Inside a configured provider only. */
function SpaceLinkGate({ publicId }: { publicId: string }) {
  const { isAuthenticated, isLoading } = useAuth0()

  // The state that gets forgotten: the SDK starts every page load
  // unauthenticated while it looks for an existing session, so treating
  // `!isAuthenticated` as "signed out" flashes the sign-in card at a member
  // before swapping it for their Space.
  if (isLoading) {
    return (
      <main className={PAGE_CLASS}>
        <p className="text-sm text-slate-600" data-testid="space-auth-loading" role="status">
          Checking your session…
        </p>
      </main>
    )
  }

  if (!isAuthenticated) {
    return (
      <main className={PAGE_CLASS}>
        <div className={CARD_CLASS} data-testid="space-sign-in">
          <h1 className="text-lg font-semibold text-slate-900">You&rsquo;ve been sent a Space</h1>
          <p className="mt-2 mb-4 text-sm text-slate-600">
            Sign in to see what this link opens. If you&rsquo;re not a member yet, you&rsquo;ll be
            able to ask for access on the next screen.
          </p>
          {/*
            Explicit rather than relying on the default: the default reads
            `window.location`, which is the same URL today, and stating it here
            means a later change to how this route is mounted cannot silently
            start returning people to the wrong place.
          */}
          <LoginControls returnTo={`/s/${publicId}`} />
        </div>
      </main>
    )
  }

  return <SpacePreviewCard publicId={publicId} />
}

type Load =
  | { kind: 'preview'; preview: SpacePreview }
  | { kind: 'not_found' }
  | { kind: 'signed_out' }
  | { kind: 'error'; message: string }
  | null

/** The preview itself, and the action it offers. Signed in by construction. */
function SpacePreviewCard({ publicId }: { publicId: string }) {
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
        // error with no way forward: a dead end on the app's only public entry
        // point is how a share link stops working for no visible reason.
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
      <main className={PAGE_CLASS}>
        <div className={CARD_CLASS} data-testid="space-sign-in">
          <h1 className="text-lg font-semibold text-slate-900">Sign in to open this Space</h1>
          <p className="mt-2 mb-4 text-sm text-slate-600">Your session has expired.</p>
          <LoginControls returnTo={`/s/${publicId}`} />
        </div>
      </main>
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
  // nothing to tell them. Until Stream 4 space-scopes bookings there is exactly
  // one calendar and it is at `/`, so that is where "into the Space" leads. When
  // a Space's calendar gets its own route, this is the line that changes.
  // `replace` so the back button returns to wherever the link was opened from
  // rather than bouncing through this redirect again.
  if (preview.status === 'member') {
    return <Navigate to="/" replace />
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
 * `member` never reaches here — it redirects a level up — so this switch covers
 * the states in which the user is still outside.
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
function NotFoundCard() {
  return (
    <main className={PAGE_CLASS}>
      <div className={CARD_CLASS} data-testid="space-not-found">
        <h1 className="text-lg font-semibold text-slate-900">{NOT_FOUND_HEADING}</h1>
        <p className="mt-2 text-sm text-slate-600">{NOT_FOUND_BODY}</p>
      </div>
    </main>
  )
}
