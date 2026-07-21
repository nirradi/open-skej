/**
 * The typed booking API client.
 *
 * Every function here returns a discriminated result and **never throws for an
 * expected outcome** — a rule denial, an overlap, a missing booking and a dead
 * network are all values, not exceptions. Callers render a branch per `outcome`
 * instead of wrapping calls in try/catch and re-deriving meaning from a status
 * code. See `types.ts` for why the status code is deliberately not exposed.
 */

import type {
  ApiInvalidRequest,
  AuthenticatedResult,
  Booking,
  CancelBookingResult,
  CreateBookingResult,
  CurrentUser,
  GetCurrentUserResult,
  ListBookingsResult,
} from './types'

/**
 * Where the FastAPI backend lives.
 *
 * Overridable with `VITE_API_BASE_URL` (set it in `app/frontend/.env.local`) so
 * the same build can point at a deployed backend. The default matches the
 * `uvicorn` default port and the origins allow-listed by the backend's CORS
 * middleware in `app/backend/app/main.py`.
 */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

/** Copy shown when the request never reached a recognisable answer. */
const NETWORK_FAILURE_MESSAGE = "We couldn't reach the server. Check your connection and try again."

const UNEXPECTED_FAILURE_MESSAGE = 'Something went wrong on our end. Please try again.'

/** Copy for a 401, or for a silent-auth failure that never reached the server. */
const UNAUTHENTICATED_MESSAGE = 'Your session has expired. Please sign in again.'

/** Copy for a 403 — the caller is known, and still not allowed. */
const FORBIDDEN_MESSAGE = "You don't have permission to do that."

/**
 * Copy for a bare 404.
 *
 * Deliberately says nothing about *why*. See `ApiNotFound` in `types.ts`: a
 * Space route answers 404 both for "no such Space" and for "not yours", so any
 * copy that distinguishes them is either a leak or a lie.
 */
const NOT_FOUND_MESSAGE = "We couldn't find that."

/**
 * What a response turned out to be, before it is narrowed to one endpoint's union.
 *
 * `discriminated` is the interesting case: the body carried an `error` key, which
 * is the contract's machine-readable outcome. The status code is not consulted to
 * produce it, precisely so that two outcomes sharing a status stay distinct.
 *
 * `unauthenticated` / `forbidden` / `not_found` are the opposite case, and the
 * reason they are status-derived is that they are the answers the *framework*
 * gives rather than the ones the domain gives. FastAPI's `HTTPException` and the
 * auth-error handler in `app/backend/app/main.py` both emit a bare `{"detail":
 * ...}` with no `error` key, so there is no discriminator to read — the status is
 * genuinely all there is. Before this existed all three fell through to `failed`
 * and arrived at the UI as "Something went wrong on our end", which is wrong
 * three different ways: an expired session is not our fault, a permission denial
 * is not an error, and a missing Space is not retryable.
 */
type Envelope =
  | { kind: 'ok'; body: unknown }
  | { kind: 'discriminated'; error: string; message: string }
  | { kind: 'invalid_request'; detail: string; raw: unknown }
  | { kind: 'unauthenticated'; message: string }
  | { kind: 'forbidden'; message: string }
  | { kind: 'not_found'; message: string }
  | { kind: 'failed'; message: string; cause?: unknown }

/**
 * Supplies the bearer token for outgoing requests.
 *
 * Resolves to the raw JWT. Rejecting is a normal, expected event — see
 * `setAccessTokenProvider`.
 */
export type AccessTokenProvider = () => Promise<string>

/**
 * The currently installed token source, or `null` for anonymous requests.
 *
 * ## Why a settable function and not an import
 *
 * The obvious implementation is to `import { useAuth0 }` here and call
 * `getAccessTokenSilently()`. That would be a mistake in two directions at once.
 *
 * It would make this module import React and the Auth0 SDK, so a file whose
 * entire job is `fetch` plus a `switch` could only be exercised inside a
 * rendered component tree wrapped in a provider — `client.test.ts` runs in the
 * `node` environment with no DOM at all and would have to become a component
 * test to keep testing URL construction. And `getAccessTokenSilently` is not
 * importable as a value anyway: it is closed over per-provider state handed out
 * by a hook, which cannot be called outside a render.
 *
 * So the dependency points the other way. This module declares what it needs,
 * `src/auth/AccessTokenBridge.tsx` supplies it at startup, and the arrow between
 * them runs from React towards the pure module rather than the reverse.
 *
 * ## Unset is a supported state, not a bug
 *
 * With no provider installed, requests simply carry no `Authorization` header.
 * That is what Stream 1's still-unauthenticated booking endpoints want, it is
 * what the existing tests exercise, and it is the honest behaviour before the
 * Auth0 SDK has finished initialising. The alternative — throwing, or blocking
 * until a provider appears — would turn "not signed in" into a crash.
 */
let accessTokenProvider: AccessTokenProvider | null = null

/**
 * Installs (or with `null`, removes) the source of the bearer token.
 *
 * Module-level rather than per-call because the token is ambient: every caller
 * in the app wants the same one, and threading it through every signature would
 * put an auth concern into the argument list of functions that have no opinion
 * about auth.
 *
 * A provider that **rejects** is expected, not exceptional. `getAccessTokenSilently`
 * rejects with `login_required` or `consent_required` whenever the session has
 * lapsed and cannot be renewed behind the scenes — which is the ordinary end of
 * every session. That is why a rejection here becomes an `unauthenticated`
 * outcome rather than propagating: it is the same fact a 401 reports, so it
 * takes the same branch, and this module's promise that expected outcomes are
 * values rather than exceptions survives contact with auth.
 */
export function setAccessTokenProvider(provider: AccessTokenProvider | null): void {
  accessTokenProvider = provider
}

/**
 * Resolves the `Authorization` header for a request.
 *
 * Three-way result, because "no provider" and "provider failed" mean opposite
 * things and collapsing them would be a security-shaped bug: no provider means
 * *send the request anonymously* (the endpoint may well be public), while a
 * failed provider means *we tried to authenticate and could not*, and quietly
 * downgrading that to an anonymous request would turn an expired session into a
 * confusing 401-from-nowhere on an endpoint the user is perfectly entitled to.
 */
async function authorizationHeader(): Promise<
  { status: 'none' } | { status: 'ok'; value: string } | { status: 'failed' }
> {
  if (!accessTokenProvider) return { status: 'none' }
  try {
    return { status: 'ok', value: `Bearer ${await accessTokenProvider()}` }
  } catch {
    return { status: 'failed' }
  }
}

/** True for a JSON object (not null, not an array). */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Flattens FastAPI's `detail` into one readable line.
 *
 * `detail` is a string for the hand-raised 400s and an array of Pydantic error
 * objects for request-validation 422s, so both shapes are handled.
 */
function formatDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (!isRecord(item)) return String(item)
      const loc = Array.isArray(item.loc) ? item.loc.join('.') : undefined
      const msg = typeof item.msg === 'string' ? item.msg : JSON.stringify(item)
      return loc ? `${loc}: ${msg}` : msg
    })
    if (parts.length > 0) return parts.join('; ')
  }
  return JSON.stringify(detail ?? null)
}

/**
 * Performs the request and classifies the response without deciding what it
 * means for any particular endpoint.
 *
 * Classification order matters. The `error` discriminator is checked **first and
 * independently of the status**, because that is the only thing that separates
 * `overlap` from `already_cancelled` (both 409) and `rule_denied` from a
 * validation failure (both 422). Status is consulted only for bodies that carry
 * no discriminator, where it is all there is.
 *
 * That ordering is also what keeps the new access kinds from stealing an
 * existing outcome. `cancelBooking`'s `not_found` arrives as a 404 carrying
 * `error: "not_found"`, so it is claimed by the discriminator branch and never
 * reaches the status check below — a Space's bare 404 and a booking's
 * discriminated one stay separately routed despite sharing a status, which is
 * the same property the two 409s have.
 */
async function request(path: string, init?: RequestInit): Promise<Envelope> {
  const authorization = await authorizationHeader()
  if (authorization.status === 'failed') {
    // No request is sent: we already know we cannot prove who we are, and an
    // anonymous retry would only produce a 401 one round trip later.
    return { kind: 'unauthenticated', message: UNAUTHENTICATED_MESSAGE }
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(authorization.status === 'ok' ? { Authorization: authorization.value } : {}),
        ...init?.headers,
      },
    })
  } catch (cause) {
    return { kind: 'failed', message: NETWORK_FAILURE_MESSAGE, cause }
  }

  let body: unknown
  try {
    body = await response.json()
  } catch (cause) {
    // A 401/403/404 whose body is not JSON is still perfectly legible — an edge
    // proxy or a gateway answering with an HTML page does not stop the status
    // from meaning what it means, and reporting an expired session as "something
    // went wrong on our end" would send the user to retry instead of to sign in.
    const byStatus = classifyByStatus(response.status)
    if (byStatus) return byStatus

    // Otherwise: a 2xx that is not JSON is just as broken as a 500 here. There
    // is no outcome to report, so it is a failure rather than a silent empty
    // result.
    return { kind: 'failed', message: UNEXPECTED_FAILURE_MESSAGE, cause }
  }

  if (response.ok) {
    return { kind: 'ok', body }
  }

  if (isRecord(body) && typeof body.error === 'string') {
    return {
      kind: 'discriminated',
      error: body.error,
      message: typeof body.message === 'string' ? body.message : UNEXPECTED_FAILURE_MESSAGE,
    }
  }

  // No discriminator. A 400 or 422 here is a malformed request — a client bug.
  if (response.status === 400 || response.status === 422) {
    const detail = isRecord(body) ? body.detail : body
    return { kind: 'invalid_request', detail: formatDetail(detail), raw: body }
  }

  const byStatus = classifyByStatus(response.status)
  if (byStatus) return byStatus

  return {
    kind: 'failed',
    message: UNEXPECTED_FAILURE_MESSAGE,
    cause: { status: response.status, body },
  }
}

/**
 * Maps the three access statuses to their envelope, or `null` for anything else.
 *
 * The server's own `detail` text is deliberately **not** forwarded into
 * `message`. FastAPI's auth handler puts the token-rejection reason there
 * ("Signature verification failed", an issuer mismatch), which is diagnostic
 * text aimed at whoever is holding the bad token, not copy for a person who
 * simply left a tab open overnight. Same argument `ApiInvalidRequest` already
 * makes for `detail`, applied one status range further along.
 */
function classifyByStatus(status: number): Envelope | null {
  switch (status) {
    case 401:
      return { kind: 'unauthenticated', message: UNAUTHENTICATED_MESSAGE }
    case 403:
      return { kind: 'forbidden', message: FORBIDDEN_MESSAGE }
    case 404:
      return { kind: 'not_found', message: NOT_FOUND_MESSAGE }
    default:
      return null
  }
}

/**
 * Folds an access outcome into `failed` for an endpoint that does not model it.
 *
 * The booking endpoints are the only callers: they predate auth and none of
 * them is behind `get_current_user` yet, so a 401 or 403 from one of them is
 * not a session problem but a sign that the deployment is misconfigured or that
 * Stream 4's space-scoping landed without these unions being widened to match.
 * Reporting that as `failed` with the specifics in `cause` says exactly that,
 * and — importantly — keeps their behaviour bit-for-bit what it was before this
 * task, since all three previously fell through to `failed` anyway.
 */
function unmodelledAccessOutcome(kind: 'unauthenticated' | 'forbidden' | 'not_found'): {
  outcome: 'failed'
  message: string
  cause: unknown
} {
  return {
    outcome: 'failed',
    message: UNEXPECTED_FAILURE_MESSAGE,
    cause: `unmodelled access outcome from an unauthenticated endpoint: "${kind}"`,
  }
}

/**
 * Turns an unrecognised discriminator into a failure.
 *
 * Reached when the server returns an `error` value an endpoint does not model —
 * a contract drift between this file and `schemas.py`. Failing loudly beats
 * guessing, since the wrong guess here is exactly the conflation the union
 * exists to prevent.
 */
function unexpectedDiscriminator(error: string): {
  outcome: 'failed'
  message: string
  cause: unknown
} {
  return {
    outcome: 'failed',
    message: UNEXPECTED_FAILURE_MESSAGE,
    cause: `unexpected error discriminator from the API: "${error}"`,
  }
}

/**
 * Rejects an invalid `Date` before it can reach `toISOString()`.
 *
 * `new Date('nonsense').toISOString()` throws a `RangeError`, which would break
 * this module's central promise that expected outcomes are values rather than
 * exceptions — a caller doing the right thing (a `switch` on `outcome`, no
 * try/catch) would get an unhandled rejection instead of a branch. An invalid
 * `Date` is a client bug, so it maps to `invalid_request`, the same variant the
 * server's own validation failures produce.
 *
 * Returns `null` when every date is valid.
 */
function rejectInvalidDates(dates: Record<string, Date>): ApiInvalidRequest | null {
  const invalid = Object.entries(dates)
    .filter(([, value]) => Number.isNaN(value.getTime()))
    .map(([name]) => name)

  if (invalid.length === 0) return null

  return {
    outcome: 'invalid_request',
    detail: `${invalid.join(' and ')} ${invalid.length === 1 ? 'is' : 'are'} not a valid date`,
    raw: null,
  }
}

/**
 * `GET /bookings?from=&to=`
 *
 * The window is half-open, `[from, to)`, and the server returns every booking
 * that *overlaps* it — a booking straddling the edge of the displayed week is
 * included, so its slot does not render as free.
 *
 * `Date`s are serialised with `toISOString()`, which always carries the `Z`
 * offset. The backend rejects a naive datetime rather than assuming UTC, so
 * going through `Date` rather than accepting caller-formatted strings makes that
 * failure unreachable.
 */
export async function listBookings(
  from: Date,
  to: Date,
  options: { includeCancelled?: boolean } = {},
): Promise<ListBookingsResult> {
  const invalid = rejectInvalidDates({ from, to })
  if (invalid) return invalid

  const query = new URLSearchParams({
    from: from.toISOString(),
    to: to.toISOString(),
  })
  if (options.includeCancelled) {
    query.set('include_cancelled', 'true')
  }

  const envelope = await request(`/bookings?${query}`)
  switch (envelope.kind) {
    case 'ok':
      return { outcome: 'ok', data: envelope.body as Booking[] }
    case 'invalid_request':
      return { outcome: 'invalid_request', detail: envelope.detail, raw: envelope.raw }
    case 'discriminated':
      return unexpectedDiscriminator(envelope.error)
    case 'unauthenticated':
    case 'forbidden':
    case 'not_found':
      return unmodelledAccessOutcome(envelope.kind)
    case 'failed':
      return { outcome: 'failed', message: envelope.message, cause: envelope.cause }
  }
}

/**
 * `POST /bookings`
 *
 * Resolves to `rule_denied` when the rule engine refused (nothing was written,
 * and `message` is friendly copy meant to be rendered verbatim) or `overlap`
 * when the interval is already taken (the calendar on screen is stale).
 */
export async function createBooking(startAt: Date, endAt: Date): Promise<CreateBookingResult> {
  const invalid = rejectInvalidDates({ start_at: startAt, end_at: endAt })
  if (invalid) return invalid

  const envelope = await request('/bookings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ start_at: startAt.toISOString(), end_at: endAt.toISOString() }),
  })

  switch (envelope.kind) {
    case 'ok':
      return { outcome: 'ok', data: envelope.body as Booking }
    case 'invalid_request':
      return { outcome: 'invalid_request', detail: envelope.detail, raw: envelope.raw }
    case 'discriminated':
      switch (envelope.error) {
        case 'rule_denied':
          return { outcome: 'rule_denied', message: envelope.message }
        case 'overlap':
          return { outcome: 'overlap', message: envelope.message }
        default:
          return unexpectedDiscriminator(envelope.error)
      }
    case 'unauthenticated':
    case 'forbidden':
    case 'not_found':
      return unmodelledAccessOutcome(envelope.kind)
    case 'failed':
      return { outcome: 'failed', message: envelope.message, cause: envelope.cause }
  }
}

/**
 * `DELETE /bookings/{id}`
 *
 * Returns 200 with the cancelled booking, not 204, so `data` carries the
 * authoritative `status` and `cancelled_at` for patching a calendar already on
 * screen — no refetch needed.
 *
 * `already_cancelled` is a distinct outcome from `not_found` and from `overlap`
 * despite sharing 409 with the latter. It means the user's own cancel already
 * landed, so the UI should treat it as success.
 */
export async function cancelBooking(bookingId: number): Promise<CancelBookingResult> {
  const envelope = await request(`/bookings/${bookingId}`, { method: 'DELETE' })

  switch (envelope.kind) {
    case 'ok':
      return { outcome: 'ok', data: envelope.body as Booking }
    case 'invalid_request':
      return { outcome: 'invalid_request', detail: envelope.detail, raw: envelope.raw }
    case 'discriminated':
      switch (envelope.error) {
        case 'not_found':
          return { outcome: 'not_found', message: envelope.message }
        case 'already_cancelled':
          return { outcome: 'already_cancelled', message: envelope.message }
        default:
          return unexpectedDiscriminator(envelope.error)
      }
    case 'unauthenticated':
    case 'forbidden':
    case 'not_found':
      return unmodelledAccessOutcome(envelope.kind)
    case 'failed':
      return { outcome: 'failed', message: envelope.message, cause: envelope.cause }
  }
}

/**
 * Performs an authenticated request and narrows it to `AuthenticatedResult`.
 *
 * The counterpart to the per-endpoint functions above: where those exist to map
 * a domain discriminator (`rule_denied`, `already_cancelled`) onto its own
 * variant, every Stream 2 route so far has no domain discriminator at all — its
 * whole answer is "here it is", "who are you", "not you", or "no such thing".
 * This spares each of them an identical seven-case `switch` while keeping the
 * same rule that callers branch on `outcome` and never see a status.
 *
 * `T` is asserted, not validated. Consistent with the rest of this module: the
 * wire types mirror `schemas.py` and a mismatch is caught by diffing the two
 * files, not by a runtime schema check the backend's own response model already
 * performs.
 */
export async function authenticatedRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<AuthenticatedResult<T>> {
  const envelope = await request(path, init)
  switch (envelope.kind) {
    case 'ok':
      return { outcome: 'ok', data: envelope.body as T }
    case 'invalid_request':
      return { outcome: 'invalid_request', detail: envelope.detail, raw: envelope.raw }
    case 'unauthenticated':
      return { outcome: 'unauthenticated', message: envelope.message }
    case 'forbidden':
      return { outcome: 'forbidden', message: envelope.message }
    case 'not_found':
      return { outcome: 'not_found', message: envelope.message }
    case 'discriminated':
      // A route reached through here has declared it has no domain outcomes, so
      // an `error` key means the contract drifted rather than that something
      // ordinary happened. Same reasoning as `unexpectedDiscriminator`.
      return unexpectedDiscriminator(envelope.error)
    case 'failed':
      return { outcome: 'failed', message: envelope.message, cause: envelope.cause }
  }
}

/**
 * `GET /me` — who the backend thinks we are.
 *
 * Worth a call even though the ID token already carries a name and an email:
 * this is the round trip that proves the *access* token is accepted by our API
 * rather than merely issued by Auth0, and it is what triggers the just-in-time
 * user upsert and the invitation claim in `get_current_user`. A user who has
 * been invited to a Space becomes a member as a side effect of this call
 * succeeding, so it is not a redundant profile fetch.
 */
export async function getCurrentUser(): Promise<GetCurrentUserResult> {
  return authenticatedRequest<CurrentUser>('/me')
}
