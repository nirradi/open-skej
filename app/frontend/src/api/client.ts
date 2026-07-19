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
  Booking,
  CancelBookingResult,
  CreateBookingResult,
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

/**
 * What a response turned out to be, before it is narrowed to one endpoint's union.
 *
 * `discriminated` is the interesting case: the body carried an `error` key, which
 * is the contract's machine-readable outcome. The status code is not consulted to
 * produce it, precisely so that two outcomes sharing a status stay distinct.
 */
type Envelope =
  | { kind: 'ok'; body: unknown }
  | { kind: 'discriminated'; error: string; message: string }
  | { kind: 'invalid_request'; detail: string; raw: unknown }
  | { kind: 'failed'; message: string; cause?: unknown }

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
 */
async function request(path: string, init?: RequestInit): Promise<Envelope> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: { Accept: 'application/json', ...init?.headers },
    })
  } catch (cause) {
    return { kind: 'failed', message: NETWORK_FAILURE_MESSAGE, cause }
  }

  let body: unknown
  try {
    body = await response.json()
  } catch (cause) {
    // A 2xx that is not JSON is just as broken as a 500 here: there is no
    // outcome to report, so it is a failure rather than a silent empty result.
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

  return {
    kind: 'failed',
    message: UNEXPECTED_FAILURE_MESSAGE,
    cause: { status: response.status, body },
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
    case 'failed':
      return { outcome: 'failed', message: envelope.message, cause: envelope.cause }
  }
}
