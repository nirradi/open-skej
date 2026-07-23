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
  AccessRequest,
  AccessRequestStatus,
  ApiInvalidRequest,
  AuthenticatedResult,
  Booking,
  CancelBookingResult,
  CancelResourceBookingResult,
  CreateBookingResult,
  CreateResourceBookingResult,
  CurrentUser,
  GetCurrentUserResult,
  Invitation,
  InvitationStatus,
  ListBookingsResult,
  ListResourceBookingsResult,
  Member,
  MembershipRole,
  MutatingResult,
  Space,
  SpacePreview,
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
  | { kind: 'conflict'; message: string }
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

  // 204 has no body by definition, so parsing one would throw and land the
  // caller in `failed` on a request that entirely succeeded. `DELETE
  // /spaces/{id}/members/{id}` is the live case: removing a member answers 204,
  // and without this the UI would report a removal that actually happened as an
  // error and leave the admin clicking it again.
  if (response.status === 204) {
    return { kind: 'ok', body: null }
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

  // An undiscriminated 409 is a domain refusal from the Space API, and its
  // `detail` is the only statement of *which* rule said no — see `ApiConflict`
  // for why this status forwards the server's copy where 401/403/404 discard
  // it. Reached only after the discriminator check above, so the booking
  // endpoints' `overlap` and `already_cancelled` are already claimed and cannot
  // be captured here.
  //
  // A 409 whose `detail` is not a string falls through to `failed` rather than
  // becoming a `conflict` with invented copy: the whole value of this variant is
  // that it carries a real explanation, and one that says "something went wrong"
  // is a failure wearing a conflict's clothes.
  if (response.status === 409 && isRecord(body) && typeof body.detail === 'string') {
    return { kind: 'conflict', message: body.detail }
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
 * Folds an outcome into `failed` for an endpoint that does not model it.
 *
 * The booking endpoints are the only callers: they predate auth and none of
 * them is behind `get_current_user` yet, so a 401 or 403 from one of them is
 * not a session problem but a sign that the deployment is misconfigured or that
 * Stream 4's space-scoping landed without these unions being widened to match.
 * Reporting that as `failed` with the specifics in `cause` says exactly that,
 * and — importantly — keeps their behaviour bit-for-bit what it was before this
 * task, since all of these previously fell through to `failed` anyway.
 *
 * `conflict` joins the list for the same reason and with the same guarantee. An
 * undiscriminated 409 on a booking route was already `failed` before this task,
 * because the two booking conflicts both carry an `error` key and are claimed by
 * the discriminator branch. Routing it here rather than widening
 * `CreateBookingResult` is what keeps that promise: `BookingPanel` and
 * `CancelPanel` switch exhaustively with no `default`, so a new variant in their
 * unions would force edits to Stream 1 components — the boundary violation that
 * task 2.8 declined for the access outcomes and that this task declines again.
 * Widening those unions belongs to Stream 4.
 */
function unmodelledAccessOutcome(
  kind: 'unauthenticated' | 'forbidden' | 'not_found' | 'conflict',
): {
  outcome: 'failed'
  message: string
  cause: unknown
} {
  return {
    outcome: 'failed',
    message: UNEXPECTED_FAILURE_MESSAGE,
    cause: `unmodelled outcome from an unauthenticated endpoint: "${kind}"`,
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
 * Turns an undiscriminated 409 into a failure, for a route that declares no
 * bare-conflict outcome at all.
 *
 * The resource-scoped booking routes only ever refuse with a discriminated
 * `error` — `overlap`, `space_archived`, `already_started`, `already_cancelled`
 * — every one of which is claimed by the `discriminated` branch before this
 * runs. A bare 409 reaching here is contract drift with `resource_bookings.py`,
 * not a domain refusal this client forgot to model, so it fails loudly the same
 * way `unexpectedDiscriminator` does rather than inventing a `conflict` outcome
 * these result unions do not have.
 */
function unexpectedConflict(message: string): {
  outcome: 'failed'
  message: string
  cause: unknown
} {
  return {
    outcome: 'failed',
    message: UNEXPECTED_FAILURE_MESSAGE,
    cause: `unexpected undiscriminated conflict from a booking route: "${message}"`,
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
    case 'conflict':
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
    case 'conflict':
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
    case 'conflict':
      return unmodelledAccessOutcome(envelope.kind)
    case 'failed':
      return { outcome: 'failed', message: envelope.message, cause: envelope.cause }
  }
}

/** The URL prefix every resource-scoped booking route sits under. */
function resourceBookingsPath(publicId: string, resourceId: number): string {
  return `/spaces/${encodeURIComponent(publicId)}/resources/${resourceId}/bookings`
}

/**
 * `GET /spaces/{public_id}/resources/{resource_id}/bookings?from=&to=`
 *
 * The scoped counterpart of `listBookings`: same half-open `[from, to)` window
 * and overlap semantics, but authenticated and authorized through
 * `require_space_role`, so `unauthenticated` / `forbidden` / `not_found` are
 * reachable here in a way the unscoped route cannot produce — see
 * `ListResourceBookingsResult`. A read has no domain refusal to add on top, so
 * this is a thin wrapper over `authenticatedRequest`.
 */
export async function listResourceBookings(
  publicId: string,
  resourceId: number,
  from: Date,
  to: Date,
  options: { includeCancelled?: boolean } = {},
): Promise<ListResourceBookingsResult> {
  const invalid = rejectInvalidDates({ from, to })
  if (invalid) return invalid

  const query = new URLSearchParams({
    from: from.toISOString(),
    to: to.toISOString(),
  })
  if (options.includeCancelled) {
    query.set('include_cancelled', 'true')
  }

  return authenticatedRequest<Booking[]>(`${resourceBookingsPath(publicId, resourceId)}?${query}`)
}

/**
 * `POST /spaces/{public_id}/resources/{resource_id}/bookings`
 *
 * The scoped counterpart of `createBooking`, widened with the access floor and
 * one more domain refusal: `space_archived`, for a create against a Resource
 * whose Space has been archived. Distinct from `overlap` even though both are
 * 409 — see `ApiSpaceArchived` — so the UI can tell "someone beat you to this
 * slot, try another" from "this venue is closed, stop offering the form".
 */
export async function createResourceBooking(
  publicId: string,
  resourceId: number,
  startAt: Date,
  endAt: Date,
): Promise<CreateResourceBookingResult> {
  const invalid = rejectInvalidDates({ start_at: startAt, end_at: endAt })
  if (invalid) return invalid

  const envelope = await request(resourceBookingsPath(publicId, resourceId), {
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
        case 'space_archived':
          return { outcome: 'space_archived', message: envelope.message }
        default:
          return unexpectedDiscriminator(envelope.error)
      }
    case 'unauthenticated':
      return { outcome: 'unauthenticated', message: envelope.message }
    case 'forbidden':
      return { outcome: 'forbidden', message: envelope.message }
    case 'not_found':
      return { outcome: 'not_found', message: envelope.message }
    case 'conflict':
      return unexpectedConflict(envelope.message)
    case 'failed':
      return { outcome: 'failed', message: envelope.message, cause: envelope.cause }
  }
}

/**
 * `DELETE /spaces/{public_id}/resources/{resource_id}/bookings/{booking_id}`
 *
 * The scoped counterpart of `cancelBooking`, widened with the access floor and
 * one more domain refusal: `already_started`, for a cancel against a booking
 * whose start time has already passed. Unlike `already_cancelled`, there is no
 * remedy — the interval is under way — so the UI should not offer a retry.
 *
 * `not_found` here covers three things the server keeps indistinguishable on
 * purpose — see `ListResourceBookingsResult`'s docstring — so this function
 * does not attempt to tell them apart either.
 */
export async function cancelResourceBooking(
  publicId: string,
  resourceId: number,
  bookingId: number,
): Promise<CancelResourceBookingResult> {
  const envelope = await request(`${resourceBookingsPath(publicId, resourceId)}/${bookingId}`, {
    method: 'DELETE',
  })

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
        case 'already_started':
          return { outcome: 'already_started', message: envelope.message }
        default:
          return unexpectedDiscriminator(envelope.error)
      }
    case 'unauthenticated':
      return { outcome: 'unauthenticated', message: envelope.message }
    case 'forbidden':
      return { outcome: 'forbidden', message: envelope.message }
    case 'not_found':
      // A bare 404 (no `error` key): the Space or the Resource is not the
      // caller's. Folded into the same variant as the discriminated
      // `not_found` above — see the function docstring.
      return { outcome: 'not_found', message: envelope.message }
    case 'conflict':
      return unexpectedConflict(envelope.message)
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
export async function mutatingRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<MutatingResult<T>> {
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
    case 'conflict':
      return { outcome: 'conflict', message: envelope.message }
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
 * The read-only counterpart: the same narrowing, minus the domain refusal.
 *
 * A `GET` in the Space API has no 409 to give — there is no rule to break by
 * looking at something — so this exists to keep that fact in the type rather
 * than leaving every read with a branch it can never take. A 409 arriving here
 * anyway is contract drift, and is reported as such instead of being handed to
 * a caller who has nowhere to put it.
 */
export async function authenticatedRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<AuthenticatedResult<T>> {
  const result = await mutatingRequest<T>(path, init)
  if (result.outcome === 'conflict') {
    return {
      outcome: 'failed',
      message: UNEXPECTED_FAILURE_MESSAGE,
      cause: `unexpected conflict from a read-only route: "${result.message}"`,
    }
  }
  return result
}

/** JSON body plus the header that makes the server parse it. */
function jsonBody(payload: unknown): RequestInit {
  return {
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
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

/**
 * `POST /spaces` — create a Space; the caller becomes its owner.
 *
 * **The `public_id` in the response is the only handle to this Space that will
 * ever exist.** There is no lookup-by-name and no directory to recover it from,
 * so a UI that discards this response has lost the Space. `listSpaces` will
 * still return it — membership is the other way back in — but the *link*, the
 * thing that lets anyone else reach it, exists nowhere but here and in that
 * list.
 */
export async function createSpace(
  name: string,
  description?: string | null,
): Promise<AuthenticatedResult<Space>> {
  return authenticatedRequest<Space>('/spaces', {
    method: 'POST',
    ...jsonBody({ name, description: description ?? null }),
  })
}

/**
 * `GET /spaces` — the Spaces this caller is a member of.
 *
 * **Not a directory.** It is worth being explicit, because the name reads like
 * one: this returns memberships, and a Space the caller has no relationship
 * with is not merely filtered out of the response, it is unreachable by any
 * route that does not already carry its unguessable `public_id`. There is no
 * endpoint that enumerates Spaces and none should be built — the link is the
 * capability, so a directory would hand away every capability at once.
 *
 * Archived Spaces are excluded unless asked for, since the common case is a
 * working list and an archived Space accepts no mutations.
 */
export async function listSpaces(
  options: { includeArchived?: boolean } = {},
): Promise<AuthenticatedResult<Space[]>> {
  const query = options.includeArchived ? '?include_archived=true' : ''
  return authenticatedRequest<Space[]>(`/spaces${query}`)
}

/**
 * `GET /spaces/{public_id}` — full detail, for members.
 *
 * A `not_found` here means "no such Space, **or** not yours", and the two are
 * indistinguishable on purpose — see `ApiNotFound`. Copy must not resolve the
 * ambiguity in either direction.
 */
export async function getSpace(publicId: string): Promise<AuthenticatedResult<Space>> {
  return authenticatedRequest<Space>(`/spaces/${encodeURIComponent(publicId)}`)
}

/**
 * `GET /spaces/{public_id}/preview` — the cold link-holder view.
 *
 * The outside of the door: name, description, and the caller's own standing
 * (`none | pending | denied | member`). No member list, no counts, no bookings —
 * everything here is disclosed to whoever the link was forwarded to.
 *
 * **Authenticated, despite being the route for people who are not members.** It
 * sits behind `get_current_user` because the status it reports is a fact about
 * *you*, and there is no "you" to report on for an anonymous caller. A signed-out
 * visitor holding a link must therefore sign in before they see even the Space's
 * name — the screen rendering this has to say so rather than showing an empty
 * card or a 401.
 *
 * `not_found` here means "no such Space" more literally than elsewhere in this
 * client — the route performs no membership check, so there is nothing for the
 * 404 to conceal. Copy must still not resolve it that way: the same UI renders
 * this result and the ones from routes that *do* hide a membership check behind
 * a 404, and a message that assumes otherwise would leak on those.
 */
export async function previewSpace(publicId: string): Promise<AuthenticatedResult<SpacePreview>> {
  return authenticatedRequest<SpacePreview>(`/spaces/${encodeURIComponent(publicId)}/preview`)
}

/**
 * `POST /spaces/{public_id}/access-requests` — ask to be let in.
 *
 * Reachable without a membership, necessarily: requiring membership to ask for
 * membership would make the door unopenable. The row it writes grants nothing by
 * itself; an admin approves it from the dashboard.
 *
 * `conflict` covers all three refusals the server distinguishes only in prose —
 * the Space is archived, you are already a member, or you already have a request
 * pending — and its `detail` is product copy meant to be shown verbatim. There is
 * no discriminator to branch on, and inventing one here would mean parsing the
 * server's sentence.
 */
export async function requestAccess(
  publicId: string,
  message?: string | null,
): Promise<MutatingResult<AccessRequest>> {
  return mutatingRequest<AccessRequest>(`/spaces/${encodeURIComponent(publicId)}/access-requests`, {
    method: 'POST',
    ...jsonBody({ message: message ?? null }),
  })
}

/** `GET /spaces/{public_id}/members` — who is in the Space. Members and up. */
export async function listMembers(publicId: string): Promise<AuthenticatedResult<Member[]>> {
  return authenticatedRequest<Member[]>(`/spaces/${encodeURIComponent(publicId)}/members`)
}

/**
 * `PATCH /spaces/{public_id}/members/{user_id}` — change a member's role.
 *
 * Three distinct refusals, and they mean different things to the person
 * clicking:
 *
 * - `forbidden` — an admin tried to grant `owner`, or to change an existing
 *   owner. Only an owner may do either, which is what stops an admin promoting
 *   themselves and taking the Space.
 * - `conflict` — the last owner cannot be demoted. The server's copy names the
 *   remedy (promote someone else first) and should be shown verbatim.
 * - `not_found` — either the Space is not yours or that user is not in it.
 */
export async function updateMemberRole(
  publicId: string,
  userId: number,
  role: MembershipRole,
): Promise<MutatingResult<Member>> {
  return mutatingRequest<Member>(`/spaces/${encodeURIComponent(publicId)}/members/${userId}`, {
    method: 'PATCH',
    ...jsonBody({ role }),
  })
}

/**
 * `DELETE /spaces/{public_id}/members/{user_id}` — remove a member.
 *
 * Resolves to `ok` with `null` data: the server answers 204 with no body, and
 * there is nothing left to describe. Same authority rules as `updateMemberRole`
 * — owner to remove an owner, and the last owner cannot be removed at all.
 */
export async function removeMember(
  publicId: string,
  userId: number,
): Promise<MutatingResult<null>> {
  return mutatingRequest<null>(`/spaces/${encodeURIComponent(publicId)}/members/${userId}`, {
    method: 'DELETE',
  })
}

/**
 * `GET /spaces/{public_id}/access-requests` — the review queue. Admin and up.
 *
 * A plain member gets `forbidden` rather than `not_found`: they are already
 * inside the Space and know it exists, so there is nothing left for a 404 to
 * conceal.
 */
export async function listAccessRequests(
  publicId: string,
  options: { status?: AccessRequestStatus } = {},
): Promise<AuthenticatedResult<AccessRequest[]>> {
  const query = options.status ? `?status=${encodeURIComponent(options.status)}` : ''
  return authenticatedRequest<AccessRequest[]>(
    `/spaces/${encodeURIComponent(publicId)}/access-requests${query}`,
  )
}

/**
 * `POST /spaces/{public_id}/access-requests/{id}/approve` — let them in.
 *
 * **Grants `member`, and takes no role argument, by design.** The approval and
 * the membership are written in one transaction, so a request is never left
 * approved without the membership that makes it mean something. An admin who
 * wants the new arrival at a higher role promotes them afterwards through
 * `updateMemberRole`, which is the one place the owner-authority and last-owner
 * invariants live — adding a role to this call would duplicate that
 * authorization logic into a second path for no product gain.
 *
 * `conflict` means the request was already decided, usually because another
 * admin got there first.
 */
export async function approveAccessRequest(
  publicId: string,
  requestId: number,
): Promise<MutatingResult<AccessRequest>> {
  return mutatingRequest<AccessRequest>(
    `/spaces/${encodeURIComponent(publicId)}/access-requests/${requestId}/approve`,
    { method: 'POST' },
  )
}

/**
 * `POST /spaces/{public_id}/access-requests/{id}/deny` — turn them down.
 *
 * Not a permanent bar: the row is kept as history rather than deleted, and only
 * *pending* requests are unique per user, so the same person may ask again.
 */
export async function denyAccessRequest(
  publicId: string,
  requestId: number,
): Promise<MutatingResult<AccessRequest>> {
  return mutatingRequest<AccessRequest>(
    `/spaces/${encodeURIComponent(publicId)}/access-requests/${requestId}/deny`,
    { method: 'POST' },
  )
}

/**
 * `GET /spaces/{public_id}/invitations` — who has been invited. Admin and up.
 *
 * Admin-only rather than member-visible because it lists the addresses of
 * people who are *not* in the Space: who is being recruited is not every
 * member's business.
 */
export async function listInvitations(
  publicId: string,
  options: { status?: InvitationStatus } = {},
): Promise<AuthenticatedResult<Invitation[]>> {
  const query = options.status ? `?status=${encodeURIComponent(options.status)}` : ''
  return authenticatedRequest<Invitation[]>(
    `/spaces/${encodeURIComponent(publicId)}/invitations${query}`,
  )
}

/**
 * `POST /spaces/{public_id}/invitations` — pre-approve an address at a role.
 *
 * **Nothing is emailed.** The row records that the address is pre-approved and
 * the inviter shares the Space's link themselves. The invitee is admitted on
 * their first login, and only when their token carries `email_verified: true` —
 * an invitation trusts the *proof* of an address, never the address as typed.
 *
 * `forbidden` means the requested role is above the caller's own; `conflict`
 * means the address is already a member or already has a pending invitation.
 * The UI should not offer a role above the caller's, but must still handle the
 * 403 — the select is a convenience and the server is the boundary.
 */
export async function createInvitation(
  publicId: string,
  email: string,
  role: MembershipRole,
): Promise<MutatingResult<Invitation>> {
  return mutatingRequest<Invitation>(`/spaces/${encodeURIComponent(publicId)}/invitations`, {
    method: 'POST',
    ...jsonBody({ email, role }),
  })
}

/**
 * `DELETE /spaces/{public_id}/invitations/{id}` — withdraw a pending invitation.
 *
 * A `DELETE` by URL shape but a status transition underneath, which is why it
 * returns the invitation rather than 204: the row survives as `revoked` so the
 * record of who invited whom is not erased along with the access, and its
 * `status` is the evidence the revocation took effect.
 *
 * `conflict` means it was already accepted or already revoked. The accepted
 * case matters: that person is a member by now, and revoking would not remove
 * the membership — succeeding here would tell the admin they had withdrawn
 * access the invitee still holds.
 */
export async function revokeInvitation(
  publicId: string,
  invitationId: number,
): Promise<MutatingResult<Invitation>> {
  return mutatingRequest<Invitation>(
    `/spaces/${encodeURIComponent(publicId)}/invitations/${invitationId}`,
    { method: 'DELETE' },
  )
}

/**
 * `POST /spaces/{public_id}/archive` — end a Space. **Owner only.**
 *
 * Restricted more tightly than the other mutations because it is the one action
 * with no inverse: there is no un-archive endpoint. An archived Space rejects
 * every subsequent mutation with a `conflict`, so the UI should stop offering
 * them once `archived_at` is set rather than letting an admin discover it one
 * refusal at a time.
 *
 * What archiving means for the bookings already made against the Space is
 * deliberately unanswered here — that is Stream 4's question. This only stamps
 * `archived_at`.
 */
export async function archiveSpace(publicId: string): Promise<MutatingResult<Space>> {
  return mutatingRequest<Space>(`/spaces/${encodeURIComponent(publicId)}/archive`, {
    method: 'POST',
  })
}
